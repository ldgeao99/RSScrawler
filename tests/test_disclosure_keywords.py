"""공시 포착에 사용하는 모든 분류가 화면 강조 API에도 포함되는지 검증."""
import ast
import asyncio
from pathlib import Path
import threading
import unittest


class DisclosureKeywordsTest(unittest.TestCase):
    def test_flat_keywords_include_positive_and_negative(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'main_fastapi.py').read_text())
        keys = next(n.value for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'KEYWORD_CATEGORY_KEYS' for t in n.targets))
        categories = ast.literal_eval(keys)
        keywords = {key: [f'{key}_keyword'] for key in categories}
        function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'get_disclosure_keywords')
        function.decorator_list = []
        namespace = {'KEYWORD_CATEGORY_KEYS': categories, 'cached_disclosure_keywords': keywords, 'db_lock': threading.Lock()}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<test>', 'exec'), namespace)
        result = asyncio.run(namespace['get_disclosure_keywords']())
        self.assertEqual(result, [value[0] for value in keywords.values()])
        self.assertEqual(asyncio.run(namespace['get_disclosure_keywords']('raw')), keywords)


if __name__ == '__main__':
    unittest.main()
