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
)
from django.utils.functional import cached_property
from pypg import TypeRegistry, Property
from pypg.property import (
    DataModifierMixin,
    PostSet,
    PropertyClass as _PropertyClass,
    PropertyType,
)
from pypg.traits import ReadOnly


class ModelMeta(PropertyType):
    def __new__(mcs, name, bases, attrs):
        cls = super().__new__(mcs, name, bases, attrs)
        cls._instances = ChainMap[int, weakref.ReferenceType](
            getattr(cls.__base__, "_instances", {})
        )
        return cls

    _instances: dict[int, weakref.ReferenceType]

    def __call__(cls, *args, **cfg):
        return (
            super().__call__( *args, **cfg)
            if (obj_id := cfg.get(PropertyClass.id.name, None)) is None
            else cls._instances[obj_id]()
        )


class PropertyClass(_PropertyClass, metaclass=ModelMeta):
    model_type: type[Model]

    def __init_subclass__(cls, model_type: type[Model] = Model, **kwargs):
        super().__init_subclass__()
        fields = {t.subject.name: t.field for t in DbField.in_type(cls)}
        model_base = getattr(cls.__base__, "model_type", model_type)
        cls.model_type = type(model_type)(
            cls.__name__,
            (model_base,),
            {
                "__module__": cls.__module__,
                **fields,
            },
        )

    __id_readonly = ReadOnly()

    def _set_id(self, obj_id):
        type(self).id.default_setter(self, obj_id)
        if obj_id is None:
            return
        type(self)._instances[obj_id] = weakref.ref(self)

        def pop_instance():
            self._instances.pop(obj_id)

        f = weakref.finalize(self, pop_instance)
        f.atexit = False

    id = Property[int](setter=_set_id, traits=[__id_readonly])

    def _create_model_instance(self):
        return (
            self.model_type()
            if self.id is None
            else self.model_type.objects.get(id=self.id)
        )

    _model_instance: Model = Property[Model](default=_create_model_instance)

    def save(self):
        self._model_instance.save()
        with self.__id_readonly.override(self):
            self.id = self._model_instance.id
            self._instance_id = self._model_instance.id


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
            for tr in p.traits:
                if isinstance(tr, cls):
                    yield tr
                    break
