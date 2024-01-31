from __future__ import annotations

import itertools
import weakref
from datetime import datetime
from typing import Any, ChainMap, Iterable, get_args, get_origin

from django.db.models import (
    Model,
    CharField,
    BooleanField,
    IntegerField,
    FloatField,
    ForeignKey,
    DateTimeField,
    CASCADE,
    JSONField,
    QuerySet,
    ManyToManyField,
)
from django.utils.functional import cached_property
from polymorphic.models import PolymorphicModel
from pypg import TypeRegistry, Locator, get_fully_qualified_name
from pypg.property import (
    DataModifierMixin,
    PostSet,
    Property,
    PropertyClass as _PropertyClass,
)

_locate = Locator()


class PropertyClass(_PropertyClass):
    model_type: type[Model]
    models: dict[type[Model], type[PropertyClass]] = {}
    instances: ChainMap[int, weakref.ReferenceType] = ChainMap[
        int, weakref.ReferenceType
    ]()
    fields: set[DbField] = {}

    def _create_model(self):
        return self.model_type()

    def _set_model(self, m: Model):
        type(self)._model_instance.default_setter(self, m)
        self._cache_instance()

    def _cache_instance(self):
        if (pk := self._model_instance.pk) is not None:
            type(self).instances[pk] = weakref.ref(self)
            f = weakref.finalize(self, lambda: self.instances.pop(pk))
            f.atexit = False

    _model_instance = Property[Model](_create_model, setter=_set_model)

    @classmethod
    def create_model(
        cls,
        model_type,
        abstract: bool = False,
        **fields,
    ):
        if "Meta" in cls.__dict__:
            fields["Meta"] = getattr(cls, "Meta")
        model_base = getattr(cls.__base__, "model_type", model_type)
        return type(model_type)(
            cls.__name__,
            (model_base,),
            {
                "__module__": cls.__module__,
                **fields,
            },
        )

    def __init_subclass__(
        cls,
        model_type: type[Model] = PolymorphicModel,
        **kwargs,
    ):
        super().__init_subclass__()
        cls.fields = {*DbField.in_type(cls)}
        fields = {
            t.subject.name: t.field
            for t in cls.fields.difference(cls.__base__.fields)
        }
        cls.model_type = cls.create_model(model_type, **fields)
        cls.models[cls.model_type] = cls
        cls.instances = ChainMap[int, weakref.ReferenceType]()
        if issubclass(cls.__base__, PropertyClass):
            cls.__base__.instances = cls.__base__.instances.new_child(
                cls.instances
            )

    @property
    def pk(self):
        return self._model_instance.pk

    def save(self):
        for dbf in type(self).fields:
            dbf.__save__(self)
        self._model_instance.save()
        for dbf in type(self).fields:
            dbf.__save_related__(self)
        self._cache_instance()
        return self

    @classmethod
    def get(cls, pk: int = None, *args, **kwargs):
        if pk is not None:
            try:
                return cls.instances[pk]()
            except KeyError:
                kwargs["pk"] = pk
        try:
            mi = cls.model_type.objects.get(*args, **kwargs)
        except cls.model_type.DoesNotExist:
            return None
        return cls(_model_instance=mi)

    @classmethod
    def from_model(cls, model: Model):
        try:
            return cls.instances[model.pk]()
        except KeyError:
            return cls(_model_instance=model)

    @classmethod
    def from_queryset(cls, query_set: QuerySet):
        for item in query_set:
            item: Model
            ptype = cls.models[type(item)]
            yield ptype.from_model(item)

    def __getattr__(self, item):
        value = getattr(self._model_instance, item)
        if isinstance(value, QuerySet):
            return self.from_queryset(value)
        try:
            p_cls = PropertyClass.models[type(value)]
        except KeyError:
            return value
        return p_cls.from_model(value)


class FieldProxy:
    registry: TypeRegistry[type[FieldProxy]] = TypeRegistry()

    @classmethod
    def create(cls, dbfield: DbField):
        if ManyToManyProxy.get_many_to_many_ref_field(
            dbfield.subject.value_type
        ):
            return ManyToManyProxy(dbfield)
        try:
            return cls.registry[dbfield.subject.value_type:](dbfield)
        except KeyError:
            return cls(dbfield)

    def __init__(self, owner: DbField):
        self.owner = owner

    def __save__(self, instance: PropertyClass):
        pass

    def __save_related__(self, instance: PropertyClass):
        pass

    def set(self, instance: PropertyClass, value):
        setattr(instance._model_instance, self.owner.subject.name, value)

    def get(self, instance: PropertyClass):
        return getattr(instance._model_instance, self.owner.subject.name)

    @cached_property
    def field(self):
        field_type, default_args, default_kwargs = self.field_map[
        self.owner.subject.value_type:
        ]
        return field_type(
            *(self.owner.field_args or default_args),
            **(self.owner.field_kwargs or default_kwargs),
        )

    field_map = TypeRegistry(
        {
            float: (FloatField, (), {}),
            int: (IntegerField, (), {}),
            bool: (BooleanField, (), {}),
            str: (CharField, (), {"max_length": 256}),
            datetime: (DateTimeField, (), {}),
            Model: (ForeignKey, (), {"on_delete": CASCADE}),
        }
    )

    def _pop_related_name(self):
        try:
            return self.owner.field_kwargs.pop('related_name')
        except KeyError:
            return '+'


@FieldProxy.registry.register_key(list, tuple, set, dict)
class CollectionProxy(FieldProxy):
    field_map = TypeRegistry(
        {
            list: (JSONField, (), {}),
            tuple: (JSONField, (), {}),
            set: (JSONField, (), {}),
            dict: (JSONField, (), {}),
        }
    )


@FieldProxy.registry.register_key(type)
class TypeProxy(FieldProxy):
    def get(self, instance: PropertyClass):
        return _locate(super().get(instance))

    def set(self, instance: PropertyClass, value: type):
        super().set(instance, get_fully_qualified_name(value))

    @cached_property
    def field(self):
        return CharField(max_length=256)


@FieldProxy.registry.register_key(PropertyClass)
class ReferenceProxy(FieldProxy):
    def get(self, instance: PropertyClass):
        member = super().get(instance)
        if member is None:
            return None
        return PropertyClass.models[type(member)].from_model(member)

    def set(self, instance: PropertyClass, value: PropertyClass):
        if value is not None:
            value = value._model_instance
        setattr(instance._model_instance, self.owner.subject.name, value)

    def __save__(self, instance: PropertyClass):
        if (attr := self.get(instance)) is not None:
            attr.save()

    @cached_property
    def field(self):
        return ForeignKey(
            self.owner.subject.value_type.model_type,
            *self.owner.field_args,
            on_delete=CASCADE,
            related_name=self._pop_related_name(),
            **self.owner.field_kwargs,
        )


class ManyToManyProxy(CollectionProxy):
    @classmethod
    def get_many_to_many_ref_field(cls, t: type):
        type_args = [
            *filter(
                lambda arg_t: arg_t is not ...,
                get_args(t),
            )
        ]
        return (
            type_args[0].model_type
            if len(type_args) == 1
               and issubclass(type_args[0], PropertyClass)
               and issubclass(get_origin(t), Iterable)
            else None
        )

    def set(self, instance: PropertyClass, value: Iterable):
        pass

    def __save__(self, instance: PropertyClass):
        pass

    def __save_related__(self, instance: PropertyClass):
        value = self.owner.subject.get(instance)
        mmf = getattr(instance._model_instance, self.owner.subject.name)
        mmf.clear()
        for item in filter(lambda i: i is not None, value):
            item.save()
            mmf.add(item._model_instance)

    def get(self, instance):
        return [*PropertyClass.from_queryset(super().get(instance).all())]

    @cached_property
    def field(self):
        return ManyToManyField(
            self.get_many_to_many_ref_field(self.owner.subject.value_type),
            *self.owner.field_args,
            related_name=self._pop_related_name(),
            **self.owner.field_kwargs,
        )


class DbField(DataModifierMixin[PostSet]):
    def __init__(self, *field_args, **field_kwargs):
        super().__init__()
        self.field_args = field_args
        self.field_kwargs = field_kwargs
        self._proxy: FieldProxy = None

    def __save__(self, instance):
        self._proxy.__save__(instance)

    def __save_related__(self, instance):
        self._proxy.__save_related__(instance)

    def __bind__(self, subject: Property):
        super().__bind__(subject)
        self._proxy = FieldProxy.create(self)
        if subject._default is None:
            subject._default = self._proxy.get

    @cached_property
    def field(self):
        return self._proxy.field

    def apply(self, instance: PropertyClass, value) -> Any:
        self._proxy.set(instance, value)

    @classmethod
    def in_type(cls, t: type[PropertyClass]) -> Iterable[DbField]:
        return itertools.chain.from_iterable(
            (
                (tr for tr in p.traits if isinstance(tr, cls))
                for p in t.properties
            )
        )
