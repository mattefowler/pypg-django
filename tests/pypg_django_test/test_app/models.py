from pypg_django import PropertyClass, Property, DbField


# Create your models here.
class TestClass(PropertyClass):
    foo = Property[float](traits=[DbField()])
    str_field = Property[str](traits=[DbField()])


class Subclass(TestClass):
    bar = Property[int](traits=[DbField()])


class ManyToManyTest(PropertyClass):
    related = Property[list[TestClass, ...]](traits=[DbField()])


class ForeignKeyTest(PropertyClass):
    related_parent = Property[TestClass](traits=[DbField()])
    related_child = Property[Subclass](traits=[DbField()])
    related_list = Property[ManyToManyTest](traits=[DbField()])


class JsonTypesTest(PropertyClass):
    list_field = Property[list[float, ...]](traits=[DbField()])
    dict_field = Property[dict[str, list[int, ...]]](traits=[DbField()])
