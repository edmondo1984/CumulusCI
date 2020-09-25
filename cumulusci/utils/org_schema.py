from logging import getLogger
from pathlib import Path
from tempfile import TemporaryDirectory
import gzip
from collections import defaultdict
from typing import Dict
from email.utils import parsedate
from contextlib import ExitStack, contextmanager

from sqlalchemy import create_engine, MetaData
from sqlalchemy.orm import create_session
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, exc
from cumulusci.utils.org_schema_models import Base, SObject, Field, FileMetadata
from cumulusci.utils.http.multi_request import CompositeParallelSalesforce

y2k = "Sat, 1 Jan 2000 00:00:01 GMT"


def zip_database(tempfile, schema_path):
    """Compress tempfile.db to schema_path.db.gz"""
    with tempfile.open("rb") as db:
        with schema_path.open("wb") as fileobj:
            with gzip.GzipFile(fileobj=fileobj) as gzipped:
                gzipped.write(db.read())


def unzip_database(gzipfile, outfile):
    """Decompress schema_path.db.gz to outfile.db"""
    with gzipfile.open("rb") as fileobj:
        with gzip.GzipFile(fileobj=fileobj) as gzipped:
            with open(outfile, "wb") as db:
                db.write(gzipped.read())


class Schema:
    """Represents an org's schema, cached from descibe() calls"""

    _last_modified_date = None

    def __init__(self, engine, schema_path):
        self.engine = engine
        Session = sessionmaker(bind=self.engine)
        self.session = Session()
        self.path = schema_path

    @property
    def sobjects(self):
        return self.session.query(SObject)

    def __getitem__(self, name):
        try:
            return self.session.query(SObject).filter_by(name=name).one()
        except exc.NoResultFound:
            raise KeyError(f"No sobject named {name}")

    def __contains__(self, name):
        return self.session.query(SObject).filter_by(name=name).all()

    def keys(self):
        return (x[0] for x in self.session.query(SObject.name).all())

    def values(self):
        return self.session.query(SObject).all()

    def items(self):
        return ((obj.name, obj) for obj in self.session.query(SObject).all())

    def block_writing(self):
        """After this method is called, the database can't be updated again"""
        # changes don't get saved back to the gzip
        # so there is no point writing to the DB
        def closed():
            raise IOError("Database is not open for writing")

        self.session._real_commit__ = self.session.commit
        self.session.commit = closed

    def close(self):
        self.session.close()

    @property
    def last_modified_date(self):
        """Date of the most recent schema update"""
        if not self._last_modified_date:
            try:
                self._last_modified_date = (
                    self.session.query(FileMetadata)
                    .filter(FileMetadata.name == "Last-Modified")
                    .one()
                    .value
                )
            except exc.NoResultFound:
                pass
        return self._last_modified_date

    def __repr__(self):
        return f"<Schema {self.path} : {self.engine}>"


def create_row(buffered_session: "BufferedSession", model, valuesdict: dict):
    buffered_session.write_single_row(model.__tablename__, valuesdict)


def populate_cache(schema, sf, last_modified_date, logger=None):
    engine = schema.engine
    metadata = Base.metadata

    sobjs = sf.describe()["sobjects"]
    sobj_names = [obj["name"] for obj in sobjs]

    full_sobjs = deep_describe(sf, last_modified_date, sobj_names)
    full_sobjs = list(full_sobjs)

    metadata.bind = engine
    metadata.reflect()

    with BufferedSession(engine, metadata) as sess:

        max_last_modified = (parsedate(last_modified_date), last_modified_date)
        for (sobj_data, last_modified) in full_sobjs:
            fields = sobj_data.pop("fields")
            sobj_data["actionOverrides"] = []
            create_row(sess, SObject, sobj_data)
            for field in fields:
                field["sobject"] = sobj_data["name"]
                create_row(sess, Field, field)
                sortable = parsedate(last_modified), last_modified
                if sortable > max_last_modified:
                    max_last_modified = sortable

        create_row(
            sess, FileMetadata, {"name": "Last-Modified", "value": max_last_modified[1]}
        )
        create_row(sess, FileMetadata, {"name": "FormatVersion", "value": 1})

    engine.execute("vacuum")
    return


class BufferedSession:
    def __init__(self, engine: Engine, metadata: MetaData, max_buffer_size: int = 1000):
        self.buffered_rows = defaultdict(list)
        self.columns = {}
        self.engine = engine
        self.metadata = metadata
        self._prepare()
        self.max_buffer_size = max_buffer_size

    def __enter__(self, *args):
        self.session = create_session(bind=self.engine, autocommit=False)
        return self

    def __exit__(self, *args):
        self.close()

    def _prepare(self):
        # Setup table info used by the write-buffering infrastructure
        assert self.metadata.tables
        self.insert_statements = {}
        for tablename, model in self.metadata.tables.items():
            self.insert_statements[tablename] = model.insert(bind=self.engine)
            self.columns[tablename] = {
                colname: None for colname in model.columns.keys()
            }

    def write_single_row(self, tablename: str, row: Dict) -> None:
        # but first, normalize it so all keys have a value. SQLite Requires it.
        row = {**self.columns[tablename], **row}

        # cache the value for later insert
        self.buffered_rows[tablename].append(row)

        # flush if buffer is full
        if len(self.buffered_rows[tablename]) > self.max_buffer_size:
            self.flush()

    def flush(self):
        for tablename, insert_statement in self.insert_statements.items():
            # seems much more efficient than line-by-line inserts.
            if self.buffered_rows[tablename]:
                self.session.execute(insert_statement, self.buffered_rows[tablename])
                self.buffered_rows[tablename] = []
        self.session.flush()

    def commit(self):
        self.flush()
        self.session.commit()

    def close(self) -> None:
        self.commit()
        self.session.close()


@contextmanager
def get_org_schema(sf, org_config, force_recache=False, logger=None):
    """
    Get a read-only representation of an org's schema.

    org_config - an OrgConfig for the relevant org
    force_recache: True - replace cache. False (default) - use/update cache is available.
    logger - replace the standard logger "cumulusci.utils.org_schema"
    """
    assert org_config.get_orginfo_cache_dir

    with org_config.get_orginfo_cache_dir(Schema.__module__) as directory:
        directory.mkdir(exist_ok=True, parents=True)
        schema_path = directory / "org_schema.db.gz"

        if force_recache and schema_path.exists():
            schema_path.unlink()

        logger = logger or getLogger(__name__)

        with ExitStack() as closer:
            tempdir = TemporaryDirectory()
            closer.enter_context(tempdir)
            tempfile = Path(tempdir.name) / "temp_org_schema.db"
            schema = None
            if schema_path.exists():
                try:
                    cleanups_on_failure = []
                    unzip_database(schema_path, tempfile)
                    cleanups_on_failure.extend([schema_path.unlink, tempfile.unlink])
                    engine = create_engine(f"sqlite:///{str(tempfile)}")

                    schema = Schema(engine, schema_path)
                    cleanups_on_failure.extend([schema.close])
                    closer.callback(schema.close)
                    assert schema.sobjects.first().name
                    schema.from_cache = True
                except Exception as e:
                    logger.warning(
                        f"Cannot read `{schema_path}`. Recreating it. Reason `{e}`."
                    )
                    schema = None
                    for cleanup_action in reversed(cleanups_on_failure):
                        cleanup_action()

            if not schema:
                engine = create_engine(f"sqlite:///{str(tempfile)}")
                Base.metadata.bind = engine
                Base.metadata.create_all()
                schema = Schema(engine, schema_path)
                closer.callback(schema.close)
                schema.from_cache = False

            populate_cache(
                schema,
                sf,
                schema.last_modified_date or y2k,
                logger,
            )
            schema.block_writing()
            # save a gzipped copy for later
            zip_database(tempfile, schema_path)
            yield schema


def deep_describe(sf, last_modified_date, objs):
    last_modified_date = last_modified_date or y2k
    with CompositeParallelSalesforce(sf, max_workers=8) as cpsf:
        responses = cpsf.do_composite_requests(
            (
                {
                    "method": "GET",
                    "url": f"/services/data/v48.0/sobjects/{obj}/describe",
                    "referenceId": f"ref{obj}",
                    "httpHeaders": {"If-Modified-Since": last_modified_date},
                }
                for obj in objs
            )
        )

        changes = (
            (response["body"], response["httpHeaders"]["Last-Modified"])
            for response in responses
            if response["httpStatusCode"] == 200
        )
        yield from changes


if __name__ == "__main__":  # pragma: no cover
    # Run this to do a smoke test of the basic functionality of saving a schema
    # from your org named "qa"

    def init_cci():
        from cumulusci.cli.runtime import CliRuntime

        from cumulusci.salesforce_api.utils import get_simple_salesforce_connection

        runtime = CliRuntime(load_keychain=True)
        name, org_config = runtime.get_org("qa")
        sf = get_simple_salesforce_connection(runtime.project_config, org_config)
        return sf, org_config, runtime

    sf, org_config, runtime = init_cci()

    with get_org_schema(sf, org_config, force_recache=True) as schema:
        print(str([obj for obj in schema.keys()])[0:100], "...")