import gc

from django.db.models import Model
# Create your tests here.
from django.test import TestCase as _TestCase

from pypg_django_test.test_app.models import (
    TestClass,
    Subclass,
    ManyToManyTest,
)
from src.pypg_django.property_class import PropertyClass


class TestCase(_TestCase):
    def tearDown(self):
        for pcls in PropertyClass.models.values():
            pcls.instances.clear()
        super().tearDown()


class Tests(TestCase):
    def test_model_fields(self):
        tcmt: type[Model] = TestClass.model_type
        self.assertEqual(len(tcmt._meta.fields), 4)

    def test_save(self):
        foo_val = 1.234
        bar_val = "asdf"
        tc = TestClass(foo=foo_val, str_field=bar_val).save()
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
        objs = [TestClass(foo=i, str_field="").save() for i in range(10)] + [
            Subclass(foo=-i, bar=i, str_field="asdf").save() for i in range(4)
        ]
        queried = [
            *TestClass.from_queryset(
                TestClass.model_type.objects.filter(foo__lt=4)
            )
        ]
        for sub in filter(lambda o: isinstance(o, Subclass), objs):
            self.assertIn(sub, queried)
            self.assertIs(sub, queried[queried.index(sub)])

    def test_create_from_queryset(self):
        nbase = 10
        nsub = 4
        for i in range(nbase):
            TestClass.model_type.objects.get_or_create(foo=i, str_field="")

        for i in range(nsub):
            Subclass.model_type.objects.get_or_create(
                foo=-i, bar=i, str_field="asdf"
            )
        queried = [
            *TestClass.from_queryset(TestClass.model_type.objects.all())
        ]
        base_count = sum(int(isinstance(obj, TestClass)) for obj in queried)
        self.assertEqual(nbase + nsub, base_count)
        sub_count = sum(int(isinstance(obj, Subclass)) for obj in queried)
        self.assertEqual(nsub, sub_count)

    def test_list_property(self):
        relatives = [
            TestClass(foo=0, str_field="").save() for _ in range(2)
        ] + [Subclass(foo=1, bar=2, str_field="").save() for _ in range(2)]
        mmt = ManyToManyTest(related=relatives)
        mmt.save()
        relative_pks = [r.pk for r in relatives]
        mmt_pk = mmt.pk
        alias = ManyToManyTest.get(pk=mmt_pk)
        self.assertIs(alias, mmt)
        del mmt
        del relatives
        for pcls in PropertyClass.models.values():
            pcls.instances.clear()
        mmt = ManyToManyTest.get(pk=mmt_pk)
        self.assertEqual(relative_pks, [r.pk for r in mmt.related])
