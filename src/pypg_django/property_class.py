from __future__ import annotations

import itertools
import weakref
from datetime import datetime
from typing import Any, ChainMap, Iterable, get_args

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
from pypg import TypeRegistry
from pypg.property import (
    DataModifierMixin,
    PostSet,
    Property,
    PropertyClass as _PropertyClass,
)


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
        **fields,
    ):
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
        self._model_instance.save()
        for dbf in type(self).fields:
            dbf.__save__(self)
        self._cache_instance()
        return self

    @classmethod
    def get(cls, *args, pk: int = None, **kwargs):
        if pk is not None:
            try:
                return cls.instances[pk]()
            except KeyError:
                kwargs["pk"] = pk

        return cls(_model_instance=cls.model_type.objects.get(*args, **kwargs))

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
            try:
                yield cls.instances[item.pk]()
            except KeyError:
                yield cls.models[type(item)](_model_instance=item)


class FieldProxy:
    registry: TypeRegistry[type[FieldProxy]] = TypeRegistry()

    @classmethod
    def create(cls, dbfield: DbField):
        if ManyToManyProxy.get_many_to_many_ref_field(
            dbfield.subject.value_type
        ):
            return ManyToManyProxy(dbfield)
        try:
            cls.registry[dbfield.subject.value_type :]
        except KeyError:
            return cls(dbfield)

    def __init__(self, owner: DbField):
        self.owner = owner

    def __save__(self, instance: PropertyClass):
        pass

    def set(self, instance: PropertyClass, value):
        setattr(instance._model_instance, self.owner.subject.name, value)

    def get(self, instance: PropertyClass):
        return getattr(instance._model_instance, self.owner.subject.name)

    @cached_property
    def field(self):
        field_type, default_args, default_kwargs = self.field_map[
            self.owner.subject.value_type :
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


@FieldProxy.registry.register_key(PropertyClass)
class ReferenceProxy(FieldProxy):
    def get(self, instance: PropertyClass):
        return PropertyClass.from_model(super().get(instance))

    @cached_property
    def field(self):
        return ForeignKey(
            PropertyClass.models[self.owner.subject.value_type],
            on_delete=CASCADE,
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
            None
            if (len(type_args) != 1 or isinstance(type_args[0], PropertyClass))
            else type_args[0].model_type
        )

    def set(self, instance: PropertyClass, value: Iterable):
        pass

    def __save__(self, instance: PropertyClass):
        value = self.owner.subject.get(instance)
        mmf = getattr(instance._model_instance, self.owner.subject.name)
        mmf.clear()
        for item in value:
            mmf.add(item._model_instance)

    def get(self, instance):
        return [*type(instance).from_queryset(super().get(instance).all())]

    @cached_property
    def field(self):
        return ManyToManyField(
            self.get_many_to_many_ref_field(self.owner.subject.value_type)
        )


class DbField(DataModifierMixin[PostSet]):
    def __init__(self, *field_args, **field_kwargs):
        super().__init__()
        self.field_args = field_args
        self.field_kwargs = field_kwargs
        self._proxy: FieldProxy = None

    def __save__(self, instance):
        self._proxy.__save__(instance)

    @property
    def field(self):
        return self._proxy.field

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
