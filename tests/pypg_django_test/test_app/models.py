from pypg_django.property import PropertyClass, Property, DbField


# Create your models here.
class TestClass(PropertyClass):
    foo = Property[float](traits=[DbField()])
    str_field = Property[str](traits=[DbField()])
