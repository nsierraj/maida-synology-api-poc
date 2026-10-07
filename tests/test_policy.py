import pytest

from synology_poc import PathPolicy, PolicyError


def test_writes_disabled_by_default():
    policy = PathPolicy(("/poc-sandbox",))
    with pytest.raises(PolicyError, match="disabled"):
        policy.check_write("/poc-sandbox/a.txt")


@pytest.mark.parametrize("path", ["/poc-sandbox/a.txt", "/poc-sandbox/x/../y", "/poc-sandbox/deep/er"])
def test_inside_root_allowed(path):
    assert PathPolicy(("/poc-sandbox",), allow_writes=True).check_write(path).startswith("/poc-sandbox/")


@pytest.mark.parametrize("path", [
    "/poc-sandbox", "/poc-sandbox/../video/x", "/video/x", "/poc-sandboxed/x", "relative/x", "",
])
def test_escapes_refused(path):
    with pytest.raises(PolicyError):
        PathPolicy(("/poc-sandbox",), allow_writes=True).check_write(path)


def test_parent_may_be_the_root():
    policy = PathPolicy(("/poc-sandbox/",), allow_writes=True)
    assert policy.check_write_parent("/poc-sandbox") == "/poc-sandbox"
    with pytest.raises(PolicyError):
        policy.check_write_parent("/video")


def test_sharing_needs_its_own_flag():
    policy = PathPolicy(("/poc-sandbox",), allow_writes=True)
    with pytest.raises(PolicyError, match="internet"):
        policy.check_sharing("/poc-sandbox/a.png")
    assert PathPolicy(("/poc-sandbox",), allow_sharing=True).check_sharing("/poc-sandbox/a.png")


def test_multiple_roots():
    policy = PathPolicy(("/poc-sandbox", "/home/work"), allow_writes=True)
    assert policy.check_write("/home/work/a")
    with pytest.raises(PolicyError):
        policy.check_write("/home/private/a")
