import gc

from django.db.models import Model
# Create your tests here.
from django.test import TestCase as _TestCase

from pypg_django_test.test_app.models import TestClass, Subclass
from src.pypg_django.property import PropertyClass


class TestCase(_TestCase):
    def tearDown(self):
        for pcls in PropertyClass._PropertyClass__models.values():
            pcls._instances.clear()
        super().tearDown()


class Tests(TestCase):
    def test_model_fields(self):
        tcmt: type[Model] = TestClass.model_type
        self.assertEqual(len(tcmt._meta.fields), 4)

    def test_save(self):
        foo_val = 1.234
        bar_val = "asdf"
        tc = TestClass(foo=foo_val, str_field=bar_val)
        tc.save()
        self.assertIsNotNone(tc.pk)
        tc_id = tc.pk
        del tc
        gc.collect()
        tc = TestClass.get(pk=tc_id)
        self.assertEqual(foo_val, tc.foo)
        self.assertEqual(bar_val, tc.str_field)

        tc_2 = TestClass.get(pk=tc_id)
        self.assertIs(tc, tc_2)

    def test_from_queryset(self):
        objs = [TestClass(foo=i, str_field="") for i in range(10)] + [
            Subclass(foo=-i, bar=i, str_field="asdf") for i in range(4)
        ]
        for obj in objs:
            obj.save()
        queried = [
            *TestClass.from_queryset(
                TestClass.model_type.objects.filter(foo__lt=4)
            )
        ]
        for sub in filter(lambda o: isinstance(o, Subclass), objs):
            self.assertIn(sub, queried)
            self.assertIs(sub, queried[queried.index(sub)])

    def test_create_from_queryset(self):
        bases = [TestClass(foo=i, str_field="") for i in range(10)]
        subs = [Subclass(foo=-i, bar=i, str_field="asdf") for i in range(4)]
        for obj in bases + subs:
            obj.save()
        queried = [
            *TestClass.from_queryset(
                TestClass.model_type.objects.filter(foo__lt=4)
            )
        ]
        for sub in subs:
            self.assertIn(sub, queried)
