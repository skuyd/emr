import pytest
from django.urls import Resolver404, resolve


@pytest.mark.parametrize('path', [
    '/glucose/',
    '/glucose/new/',
    '/glucose/sources/',
    '/glucose/import/labs/00000000-0000-0000-0000-000000000001/',
    '/glucose/import/nursing/00000000-0000-0000-0000-000000000001/1/',
    '/glucose/00000000-0000-0000-0000-000000000001/',
    '/glucose/00000000-0000-0000-0000-000000000001/edit/',
    '/glucose/00000000-0000-0000-0000-000000000001/delete/',
    '/glucose/00000000-0000-0000-0000-000000000001/undo/',
    '/glucose/00000000-0000-0000-0000-000000000001/recheck/',
])
def test_glucose_routes_are_removed(path):
    with pytest.raises(Resolver404):
        resolve(path)
