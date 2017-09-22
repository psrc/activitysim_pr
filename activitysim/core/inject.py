import logging

import orca

_DECORATED_STEPS = {}
_DECORATED_TABLES = {}
_DECORATED_COLUMNS = {}
_DECORATED_INJECTABLES = {}

PASS_THROUGH = False

logger = logging.getLogger(__name__)


def step():

    def decorator(func):
        name = func.__name__

        logger.info("inject step %s" % name)
        print "inject step %s" % name

        assert not _DECORATED_STEPS.get(name, False)
        _DECORATED_STEPS[name] = func

        if PASS_THROUGH:
            func = orca.step()(func)
        else:
            orca.add_step(name, func)

        return func
    return decorator


def table():

    def decorator(func):
        name = func.__name__

        logger.info("inject table %s" % name)
        print "inject table %s" % name

        assert not _DECORATED_TABLES.get(name, False)
        _DECORATED_TABLES[name] = func

        if PASS_THROUGH:
            func = orca.table()(func)
        else:
            orca.add_table(name, func)

        return func

    return decorator


def column(table_name, cache=True):
    def decorator(func):
        name = func.__name__

        logger.info("inject column %s.%s" % (table_name, name))
        print "inject column %s.%s" % (table_name, name)

        column_key = (table_name, name)

        assert not _DECORATED_COLUMNS.get(column_key, False)
        _DECORATED_COLUMNS[column_key] = {'func': func, 'cache': cache}

        if PASS_THROUGH:
            func = orca.column(table_name, cache=cache)(func)
        else:
            orca.add_column(table_name, name, func, cache=cache)

        return func
    return decorator


def injectable(cache=False):
    def decorator(func):
        name = func.__name__

        logger.info("inject injectable %s" % name)
        print "inject injectable %s" % name

        assert not _DECORATED_INJECTABLES.get(name, False), "injectable '%s' already defined" % name
        _DECORATED_INJECTABLES[name] = func

        if PASS_THROUGH:
            func = orca.injectable(cache=cache)(func)
        else:
            orca.add_injectable(name, func, cache=cache)

        return func
    return decorator


def merge_tables(target, tables, columns=None):
    return orca.merge_tables(target, tables, columns)


def add_table(table_name, table, cache=False):
    return orca.add_table(table_name, table, cache=cache)


def add_column(table_name, column_name, column, cache=False):
    return orca.add_column(table_name, column_name, column, cache=cache)


def add_injectable(name, injectable, cache=False):
    return orca.add_injectable(name, injectable, cache=cache)


def broadcast(cast, onto, cast_on=None, onto_on=None, cast_index=False, onto_index=False):
    return orca.broadcast(cast, onto,
                          cast_on=cast_on, onto_on=onto_on,
                          cast_index=cast_index, onto_index=onto_index)


def get_table(name):
    return orca.get_table(name)


# we want to allow None (any anyting else) as a default value, so just choose an improbable string
_NO_DEFAULT = 'throw error if missing'


def get_injectable(name, default=_NO_DEFAULT):

    if orca.is_injectable(name) or default == _NO_DEFAULT:
        return orca.get_injectable(name)
    else:
        return default


def reinject_decorated_tables():
    """
    reinject the decorated tables (and columns)
    """

    # need to clear any non-decorated tables that were added during the previous run
    orca.orca._TABLES.clear()
    orca.orca._COLUMNS.clear()
    orca.orca._TABLE_CACHE.clear()
    orca.orca._COLUMN_CACHE.clear()

    for name, func in _DECORATED_TABLES.iteritems():
        orca.add_table(name, func)

    for column_key, args in _DECORATED_COLUMNS.iteritems():
        table_name, column_name = column_key
        orca.add_column(table_name, column_name, args['func'], cache=args['cache'])
