from ucmodel.sqltext import (
    canonical,
    declared_properties,
    declares_clustering,
    ident,
    qualified,
    split_statements,
    string,
    strip_catalog,
)


def test_split_ignores_semicolons_in_strings_comments_and_bodies():
    text = """
    -- a leading comment; with a semicolon
    CREATE TABLE s.t (a STRING COMMENT 'x;y', b STRING COMMENT 'it\\'s; fine');
    /* block; comment */
    CREATE FUNCTION s.f(x STRING) RETURNS STRING LANGUAGE PYTHON AS $$
    return x + ';'
    $$;
    SELECT `odd;name` FROM s.t;
    -- only a comment at the end;
    """
    statements = split_statements(text)
    assert len(statements) == 3
    assert statements[0].startswith("-- a leading comment")
    assert statements[0].endswith("COMMENT 'it\\'s; fine')")
    assert "return x + ';'" in statements[1]
    assert statements[2] == "SELECT `odd;name` FROM s.t"


def test_strip_catalog_only_touches_three_part_names_in_the_bound_catalog():
    text = (
        "SELECT w.a, workspace.b FROM workspace.sales.orders AS w "
        "JOIN `workspace`.sales.`odd name` USING (id) "
        "JOIN other.sales.x ON TRUE WHERE w.c = 'workspace.sales.orders'"
    )
    assert strip_catalog(text, "workspace") == (
        "SELECT w.a, workspace.b FROM sales.orders AS w "
        "JOIN sales.`odd name` USING (id) "
        "JOIN other.sales.x ON TRUE WHERE w.c = 'workspace.sales.orders'"
    )


def test_strip_catalog_leaves_a_four_part_field_path_alone():
    assert strip_catalog("SELECT t.workspace.a.b FROM t", "workspace") == (
        "SELECT t.workspace.a.b FROM t"
    )


def test_canonical_ignores_formatting_but_not_string_case():
    a = "SELECT a,\n  b FROM t -- note\nWHERE x = 'Ab';"
    b = "select a, b\nfrom T where X = 'Ab'"
    assert canonical(a) == canonical(b)
    assert canonical("SELECT 'Ab'") != canonical("SELECT 'ab'")


def test_declared_properties_reads_quoted_and_bare_keys():
    statement = """CREATE TABLE s.t (a INT)
    TBLPROPERTIES ('delta.appendOnly' = 'true', delta.logRetentionDuration = 'interval 30 days',
                   'my.owner' = 'x')"""
    assert declared_properties(statement) == {
        "delta.appendOnly", "delta.logRetentionDuration", "my.owner"}
    assert declared_properties("CREATE SCHEMA s WITH DBPROPERTIES ('k' = 'v')") == {"k"}
    assert declared_properties("CREATE TABLE s.t (a INT)") == set()


def test_declares_clustering():
    assert declares_clustering("CREATE TABLE s.t (a INT) CLUSTER BY AUTO")
    assert declares_clustering("CREATE TABLE s.t (a INT)\nCLUSTER\nBY (a)")
    assert not declares_clustering("CREATE TABLE s.t (a INT) COMMENT 'cluster by a'")


def test_quoting():
    assert ident("orders") == "orders"
    assert ident("Line Number") == "`Line Number`"
    assert ident("order") == "`order`"
    assert ident("we`ird") == "`we``ird`"
    assert qualified("sales.order") == "sales.`order`"
    assert string("O'Brien \\ x") == "'O\\'Brien \\\\ x'"
