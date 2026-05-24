from brigade import registry


def test_builtin_stations_present():
    names = {s.name for s in registry.all_stations()}
    assert {"core", "memory", "guard"} <= names


def test_all_builtins_expose_a_doctor():
    for s in registry.all_stations():
        assert callable(s.doctor), f"{s.name} has no doctor"


def test_resolve_by_name_and_alias():
    assert registry.resolve("memory").name == "memory"
    assert registry.resolve("garde").name == "memory"
    assert registry.resolve("pass").name == "guard"
    assert registry.resolve("nope") is None


def test_stations_declare_attached_tools():
    from brigade import registry
    memory = registry.resolve("memory")
    guard = registry.resolve("guard")
    tokens = registry.resolve("tokens")
    assert set(memory.tools) == {"memory-doctor", "bootstrap-doctor"}
    assert set(guard.tools) == {"content-guard"}
    assert tokens is not None and set(tokens.tools) == {"tokenjuice"}
