from pypg_django import PropertyClass, Property, DbField


# Create your models here.
class TestClass(PropertyClass):
    foo = Property[float](traits=[DbField()])
    str_field = Property[str](traits=[DbField()])


class Subclass(TestClass):
    bar = Property[int](traits=[DbField()])


class ManyToManyTest(PropertyClass):
    related = Property[list[TestClass, ...]](traits=[DbField()])
