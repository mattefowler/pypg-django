from __future__ import annotations

import itertools
import operator
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from threading import current_thread
from typing import Any, ChainMap, Iterable, get_args, get_origin
from weakref import ReferenceType, ref, finalize

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
    Field,
)
from django.utils.functional import cached_property
from polymorphic.models import PolymorphicModel
from pypg import TypeRegistry, Locator, get_fully_qualified_name
from pypg.property import (
    DataModifierMixin,
    PostSet,
    Property,
    PropertyClass as _PropertyClass
)

_locate = Locator()


class PropertyClass(_PropertyClass):
    model_type: type[Model]
    models: dict[type[Model], type[PropertyClass]] = {}
    instances: ChainMap[int, ReferenceType] = ChainMap[
        int, ReferenceType
    ]()
    fields: set[DbField] = {}
    __persistance_contexts: dict[int, set[PropertyClass]] = defaultdict(set)

    def __init__(self, save: bool | None = None, **cfg):
        super().__init__(**cfg)
        if save is not True:
            if (t_id := current_thread().native_id) in self.__persistance_contexts:
                self.__persistance_contexts[t_id].add(self)
                save = False
            else:
                save = self.save_on_create
        if save:
            self.save()

    def _create_model(self):
        return self.model_type()

    def _set_model(self, m: Model):
        type(self)._model_instance.default_setter(self, m)
        self._cache_instance()

    def _cache_instance(self):
        if (pk := self._model_instance.pk) is not None:
            type(self).instances[pk] = ref(self)
            f = finalize(self, lambda: self.instances.pop(pk))
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

    has_many_to_many: bool = False

    def __init_subclass__(
        cls,
        model_type: type[Model] = PolymorphicModel,
        save_on_create: bool = True,
        **kwargs,
    ):
        super().__init_subclass__()
        cls.has_many_to_many = any(isinstance(dbf.field, ManyToManyProxy) for dbf in cls.fields)
        cls.save_on_create = save_on_create
        cls.fields = {*DbField.in_type(cls)}
        fields = {
            t.subject.name: t.field
            for t in cls.fields.difference(cls.__base__.fields)
        }
        cls.can_bulk_create = not (cls.__base__.fields or cls.has_many_to_many)
        cls.model_type = cls.create_model(model_type, **fields)
        cls.models[cls.model_type] = cls
        cls.instances = ChainMap[int, ReferenceType]()
        if issubclass(cls.__base__, PropertyClass):
            cls.__base__.instances = cls.__base__.instances.new_child(
                cls.instances
            )

    @property
    def pk(self):
        return self._model_instance.pk

    def save(self):
        # if not (recurse or self.modified):
        #     return
        # self.modified = False
        # for dbf in type(self).fields:
        #     dbf.__save__(self)
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

    @cached_property
    def modified(self):
        return True

    @classmethod
    @contextmanager
    def persist(cls):
        thread_id = current_thread().native_id
        instances = cls.__persistance_contexts[thread_id]
        original_instances = instances.copy()
        try:
            yield
            new_instances = instances - original_instances
            instances.difference_update(new_instances)
            unsaved = [*filter(lambda o: o.pk is None, new_instances)]
            requires_post_save = [*filter(operator.attrgetter("has_many_to_many"), new_instances)]
            cls_types = PropertyClass.sort_reference_order(*{type(u) for u in unsaved})
            for obj_type, objs in itertools.groupby(
                sorted(unsaved, key=lambda u: cls_types.index(type(u))), key=type
            ):
                obj_type: type[PropertyClass]
                model_type = obj_type.model_type
                if obj_type.can_bulk_create:
                    model_type.objects.bulk_create([obj._model_instance for obj in objs])
                else:
                    for obj in objs:
                        obj.save()
                        try:
                            requires_post_save.remove(obj)
                        except ValueError:
                            pass
            for obj in requires_post_save:
                obj.save()
        finally:
            if not instances:
                cls.__persistance_contexts.pop(thread_id)

    @classmethod
    def refers_to(cls, other: type[PropertyClass]) -> bool:
        pcls_fields: list[Property[PropertyClass]] = [dbf.subject for dbf in cls.fields if
            isinstance(dbf.subject.value_type, type) and issubclass(dbf.subject.value_type, PropertyClass)
        ]
        for p in pcls_fields:
            if issubclass(other, p.value_type):
                return True
        for p in pcls_fields:
            if p.value_type.refers_to(other):
                return True
        return False

    @staticmethod
    def sort_reference_order(*cls: type[PropertyClass]):
        cls = {*cls}
        refs = {c: {o: c.refers_to(o) for o in cls - {c}} for c in cls}
        order = []
        while refs:
            least_referred = min(refs, key=lambda c: sum(refs[c].values()))
            refs.pop(least_referred)
            order.append(least_referred)
            for rd in refs.values():
                rd.pop(least_referred)
        return order


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

    def get_field_args(self, field_type) -> dict[str, Any]:
        try:
            kwargs = self.default_field_args[field_type:].copy()
        except KeyError:
            return self.owner.field_kwargs.copy()
        kwargs.update(self.owner.field_kwargs)
        return kwargs

    def get_field_type(self):
        return self.field_map[self.owner.subject.value_type:]

    @cached_property
    def field(self) -> Field:
        field_type = self.get_field_type()
        return field_type(**self.get_field_args(field_type))

    field_map = TypeRegistry(
        {
            float: FloatField,
            int: IntegerField,
            bool: BooleanField,
            str: CharField,
            datetime: DateTimeField,
            Model: ForeignKey,
        }
    )
    default_field_args = TypeRegistry(
        {CharField: {"max_length": 256}, ForeignKey: {"on_delete": CASCADE}}
    )

    def _pop_related_name(self):
        try:
            return self.owner.field_kwargs.pop("related_name")
        except KeyError:
            return "+"


@FieldProxy.registry.register_key(list, tuple, set, dict)
class CollectionProxy(FieldProxy):
    field_map = TypeRegistry({t: JSONField for t in (list, tuple, set, dict)})


@FieldProxy.registry.register_key(type)
class TypeProxy(FieldProxy):
    def get(self, instance: PropertyClass):
        return _locate(super().get(instance))

    def set(self, instance: PropertyClass, value: type):
        super().set(instance, get_fully_qualified_name(value))

    @cached_property
    def field(self):
        return CharField(max_length=256)


def pascal_to_snake(pascal_cased: str):
    capitals: set[str] = {*filter(str.isupper, pascal_cased)}
    result = pascal_cased
    for capital in capitals:
        result = result.replace(capital, "_" + capital.lower())
    return result[1:] if result[0] == "_" else result


related_name = "related_name"


@FieldProxy.registry.register_key(PropertyClass)
class ReferenceProxy(FieldProxy):

    @cached_property
    def reference_type(self):
        return self.owner.subject.value_type

    def get(self, instance: PropertyClass):
        try:
            return instance.__dict__[self.owner.subject.attribute_key]
        except KeyError:
            member = super().get(instance)
            if member is None:
                return None
            return PropertyClass.models[type(member)].from_model(member)

    def set(self, instance: PropertyClass, value: PropertyClass):
        self.owner.subject.default_setter(instance, value)
        if value is not None:
            value = value._model_instance
        super().set(instance, value)

    def __save__(self, instance: PropertyClass):
        if (attr := self.get(instance)) is not None and attr.pk is None:
            attr.save()

    def get_field_args(self, field_type) -> dict[str, Any]:
        args = super().get_field_args(field_type)
        args[related_name] = self.get_related_name()
        return args

    @cached_property
    def field(self):
        return ForeignKey(
            self.owner.subject.value_type.model_type,
            **self.get_field_args(ForeignKey),
        )

    @cached_property
    def default_related_name(self):
        composing_type_name = self.owner.subject.declaring_type.__name__
        value_type_name = self.owner.subject.value_type.__name__
        attr_name = self.owner.subject.name
        return f"{pascal_to_snake(composing_type_name)}_{attr_name}_{pascal_to_snake(value_type_name)}_set"

    def get_related_name(self):
        return self.owner.field_kwargs.get(
            related_name, self.default_related_name
        )


class ManyToManyProxy(ReferenceProxy):
    @cached_property
    def reference_type(self):
        return self.get_many_to_many_ref_field(self.owner.subject.value_type)

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
        if {v.pk for v in value} == {item.pk for item in mmf.all()}:
            return
        mmf.clear()
        for item in filter(lambda i: i is not None, value):
            if item.pk is None:
                item.save()
            mmf.add(item._model_instance)

    def get(self, instance):
        try:
            return instance.__dict__[self.owner.subject.attribute_key]
        except KeyError:
            return [*PropertyClass.from_queryset(FieldProxy.get(self, instance).all())]

    @cached_property
    def field(self):
        return ManyToManyField(
            self.reference_type,
            **self.get_field_args(ManyToManyField),
        )


class DbField(DataModifierMixin[PostSet]):
    def __init__(self, **field_kwargs):
        super().__init__()
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
