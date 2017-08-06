# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""" Inventory storage implementation. """

import datetime
import json

from sqlalchemy import create_engine
from sqlalchemy import Column
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import BigInteger
from sqlalchemy import Date
from sqlalchemy import Integer

from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

from google.cloud.security.inventory2.storage import Storage as BaseStorage

BASE = declarative_base()
CURRENT_SCHEMA = 1


class InventoryState(object):
    """Possible states for inventory."""

    SUCCESS = "SUCCESS"
    RUNNING = "RUNNING"
    FAILURE = "FAILURE"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    TIMEOUT = "TIMEOUT"
    CREATED = "CREATED"


class InventoryIndex(BASE):
    """Represents a GCP inventory."""

    __tablename__ = 'inventory_index'

    id = Column(Integer(), primary_key=True, autoincrement=True)
    start_time = Column(Date)
    complete_time = Column(Date)
    status = Column(String)
    schema_version = Column(BigInteger())
    progress = Column(String(255))
    counter = Column(Integer)

    @classmethod
    def _utcnow(self):
        return datetime.datetime.utcnow()

    def __repr__(self):
        return """<{}(id='{}', version='{}', timestamp='{}')>""".format(
            self.__class__.__name__,
            self.id,
            self.schema_version,
            self.cycle_timestamp)

    @classmethod
    def create(cls):
        return InventoryIndex(
            start_time=cls._utcnow(),
            status=InventoryState.CREATED,
            schema_version=CURRENT_SCHEMA,
            counter=0)

    def complete(self):
        self.complete_time = InventoryIndex._utcnow()
        self.status = InventoryState.SUCCESS

    def add_warning(self, session, warning):
        """Add a warning to the inventory.

        Args:
            warning (str): Warning message
        """

        warning_message = '{}\n'.format(warning)
        if not self.warnings:
            self.warnings = warning_message
        else:
            self.warnings += warning_message
        session.add(self)
        session.flush()

    def set_error(self, session, message):
        """Indicate a broken import."""

        self.state = "BROKEN"
        self.message = message
        session.add(self)
        session.flush()


class Inventory(BASE):
    """Resource inventory table."""

    __tablename__ = 'inventory'

    index = Column(BigInteger(), primary_key=True)
    resource_key = Column(String(1024), primary_key=True)
    resource_type = Column(String(1024))
    resource_data = Column(Text())
    iam_policy = Column(Text())
    gcs_policy = Column(Text())
    other = Column(Text())

    @classmethod
    def from_resource(cls, index, resource):
        return Inventory(
            index=index.id,
            resource_key=resource.key(),
            resource_type=resource.type(),
            resource_data=json.dumps(resource.data()),
            iam_policy=json.dumps(resource.getIamPolicy()),
            gcs_policy=json.dumps(resource.getGCSPolicy()),
            other=None)

    def __repr__(self):
        return """<{}(index='{}', key='{}', type='{}')>""".format(
            self.__class__.__name__,
            self.index,
            self.resource_key,
            self.resource_type)


class BufferedDbWriter(object):
    """Buffered db writing."""

    def __init__(self, session, max_size=1024):
        self.session = session
        self.buffer = []
        self.max_size = max_size

    def add(self, obj):
        self.buffer.append(obj)
        if self.buffer >= self.max_size:
            self.flush()

    def flush(self):
        self.session.add_all(self.buffer)
        self.session.flush()
        self.buffer = []


class Storage(BaseStorage):
    """Inventory storage."""

    def __init__(self, db_connect_string, existing_id=None):
        engine = create_engine(db_connect_string, pool_recycle=7200)
        BASE.metadata.create_all(engine)
        session = sessionmaker(bind=engine)
        self.session = session()
        self.opened = False
        self.index = None
        self.buffer = BufferedDbWriter(self.session)
        self._existing_id = existing_id

    def _require_opened(self):
        if not self.opened:
            raise Exception('Storage is not opened')

    def _open(self, existing_id):
        return (
            self.session.query(InventoryIndex)
            .filter(InventoryIndex.id == existing_id)
            .one())

    def open(self, existing_id=None):
        if self.opened:
            raise Exception('open called before')

        if existing_id or self._existing_id:
            self.index = self._open(existing_id)

        try:
            self.index = InventoryIndex.create()
            self.session.add(self.index)
        except Exception:
            self.session.rollback()
            raise
        else:
            self.opened = True
            self.session.flush()
            return self.index.id

    def close(self):
        if not self.opened:
            raise Exception('not open')

        try:
            self.session.commit()
        except Exception:
            raise
        else:
            self.opened = False

    def write(self, resource):
        self.buffer.add(
            Inventory.from_resource(
                self.index,
                resource))
        self.index.counter += 1

    def read(self, resource_key):
        self.buffer.flush()
        return (
            self.session.query(Inventory)
            .filter(Inventory.index == self.index.id)
            .filter(Inventory.resource_key == resource_key)
            .one())

    def error(self, message):
        self.index.set_error(self.session, message)

    def warning(self, message):
        self.index.add_warning(self.session, message)

    def iterinventory(self, type_list=[]):
        base_query = (
            self.session.query(Inventory)
            .filter(Inventory.index == self.index.id))

        if type_list:
            for res_type in type_list:
                qry = (base_query
                       .filter(Inventory.resource_type == res_type))
                for resource in qry.yield_per(1024):
                    yield resource
        else:
            for resource in base_query.yield_per(1024):
                yield resource

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, type_p, value, tb):
        self.close()