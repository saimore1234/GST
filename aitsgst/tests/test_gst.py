import unittest

from aitsgst.core import gst


class TestGst(unittest.TestCase):
	def test_gstin_format(self):
		self.assertTrue(gst.is_valid_gstin("27AAPFU0939F1ZV"))
		self.assertTrue(gst.is_valid_gstin(" 27aapfu0939f1zv "))  # trimmed + upper-cased
		self.assertFalse(gst.is_valid_gstin("27AAPFU0939F1Z"))  # 14 chars
		self.assertFalse(gst.is_valid_gstin("27AAPFU0939F1AV"))  # no 'Z' at position 14
		self.assertFalse(gst.is_valid_gstin(None))
		self.assertFalse(gst.is_valid_gstin(""))

	def test_state_code(self):
		self.assertEqual(gst.state_code_of_gstin("24AAPFU0939F1ZV"), "24")
		self.assertIsNone(gst.state_code_of_gstin("bad"))

	def test_place_of_supply(self):
		self.assertTrue(gst.is_valid_place_of_supply("24-Gujarat"))
		self.assertTrue(gst.is_valid_place_of_supply("26-Dadra and Nagar Haveli and Daman and Diu"))
		self.assertFalse(gst.is_valid_place_of_supply("24-Maharashtra"))
		self.assertFalse(gst.is_valid_place_of_supply("Gujarat"))
		self.assertFalse(gst.is_valid_place_of_supply(None))

	def test_hsn(self):
		self.assertEqual(gst.hsn_digits("7210.41.00"), "72104100")
		self.assertEqual(gst.hsn_digits(" 3208-10 "), "320810")
		self.assertIsNone(gst.hsn_digits(" . "))
		self.assertTrue(gst.is_standard_hsn("72104100"))
		self.assertFalse(gst.is_standard_hsn("72104"))
