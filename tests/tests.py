from django.test import TestCase
from pypg_django_test.test_app.models import TestClass


class Tests(TestCase):
    def test_save(self):
        foo_val = 1.234
        bar_val = 'asdf'
        tc = TestClass(foo=foo_val, bar=bar_val)
        tc.save()
        self.assertIsNotNone(tc.id)
        tc_id = tc.id
        del tc
        tc = TestClass(id=tc_id)
        self.assertEqual(foo_val, tc.foo)
        self.assertEqual(bar_val, tc.bar)
