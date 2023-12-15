from __future__ import annotations

import weakref
from datetime import datetime
from typing import Any, ChainMap, Iterable

from django.db.models import (
    Model,
    Field,
    CharField,
    BooleanField,
    IntegerField,
    FloatField,
    ForeignKey,
    DateTimeField,
    CASCADE,
    JSONField,
    QuerySet,
)
from django.utils.functional import cached_property
from polymorphic.models import PolymorphicModel
from pypg import TypeRegistry, Property
from pypg.property import (
    DataModifierMixin,
    PostSet,
    PropertyClass as _PropertyClass,
)


class PropertyClass(_PropertyClass):
    model_type: type[Model]
    __models: dict[type[Model], type[PropertyClass]] = {}
    _instances: ChainMap[int, weakref.ReferenceType]

    def _create_model(self):
        return self.model_type()

    _model_instance = Property[Model](_create_model)

    @classmethod
    def create_model(cls, model_type=PolymorphicModel):
        fields = {t.subject.name: t.field for t in DbField.in_type(cls)}
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
        cls, model_type: type[Model] = PolymorphicModel, **kwargs
    ):
        super().__init_subclass__()
        cls.model_type = cls.create_model(model_type)
        cls.__models[cls.model_type] = cls
        cls._instances = ChainMap[int, weakref.ReferenceType]()
        if (
            base_instances := getattr(cls.__base__, "_instances", None)
        ) is not None:
            cls.__base__._instances = base_instances.new_child(cls._instances)

    @property
    def pk(self):
        return self._model_instance.pk

    def save(self):
        self._model_instance.save()
        pk = self._model_instance.pk
        type(self)._instances[pk] = weakref.ref(self)

        def pop_instance():
            self._instances.pop(pk)

        f = weakref.finalize(self, pop_instance)
        f.atexit = False

    @classmethod
    def get(cls, *args, pk: int = None, **kwargs):
        if pk is not None:
            try:
                return cls._instances[pk]()
            except KeyError:
                pass
        return cls(_model_instance=cls.model_type.objects.get(*args, **kwargs))

    @classmethod
    def from_queryset(cls, query_set: QuerySet):
        for item in query_set:
            item: Model
            try:
                yield cls._instances[item.pk]()
            except KeyError:
                yield cls.__models[type(item)](_model_instance=item)


class DbField(DataModifierMixin[PostSet]):
    def __init__(self, *field_args, **field_kwargs):
        super().__init__()
        self._field_args = field_args
        self._field_kwargs = field_kwargs

    def _get_from_model(self, instance):
        return getattr(instance._model_instance, self.subject.name)

    def __bind__(self, subject: Property):
        super().__bind__(subject)
        if subject._default is None:
            subject._default = self._get_from_model
        field_type, default_args, default_kwargs = self.field_map[
            self.subject.value_type :
        ]
        self.field = field_type(
            *(self._field_args or default_args),
            **(self._field_kwargs or default_kwargs),
        )

    def apply(self, instance: PropertyClass, value) -> Any:
        setattr(instance._model_instance, self.subject.name, value)

    field_map = TypeRegistry[
        tuple[
            type[Field],
            DEFAULT_FIELD_ARGS := tuple[Any, ...],
            DEFAULT_FIELD_KWARGS := dict[str, Any],
        ]
    ](
        {
            float: (FloatField, (), {}),
            int: (IntegerField, (), {}),
            bool: (BooleanField, (), {}),
            str: (CharField, (), {"max_length": 256}),
            PropertyClass: (ForeignKey, (), {"on_delete": CASCADE}),
            Model: (ForeignKey, (), {"on_delete": CASCADE}),
            list: (JSONField, (), {}),
            tuple: (JSONField, (), {}),
            set: (JSONField, (), {}),
            dict: (JSONField, (), {}),
            datetime: (DateTimeField, (), {}),
        }
    )

    @cached_property
    def field_type(self) -> type[Field]:
        return self.field_map[self.subject.value_type :]

    @classmethod
    def in_type(cls, t: type[PropertyClass]) -> Iterable[DbField]:
        for p in t.properties:
            p: Property
            if not p.declaring_type is t:
                continue
            for tr in p.traits:
                if isinstance(tr, cls):
                    yield tr
                    break
