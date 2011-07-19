# Copyright (c) 2010-2011, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version 3
# only, as published by the Free Software Foundation.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License version 3 for more details
# (a copy is included in the LICENSE file that accompanied this code).
#
# You should have received a copy of the GNU Lesser General Public License
# version 3 along with OpenQuake.  If not, see
# <http://www.gnu.org/licenses/lgpl-3.0.txt> for a copy of the LGPLv3 License.


# vim: tabstop=4 shiftwidth=4 softtabstop=4
"""
Base classes for the output methods of the various codecs.
"""

import logging
from os.path import basename

from db.alchemy.models import OqJob, Output

LOGGER = logging.getLogger('serializer')
LOGGER.setLevel(logging.DEBUG)


class FileWriter(object):
    """Simple output half of the codec process."""

    def __init__(self, path):
        self.path = path
        self.file = None
        self._init_file()
        self.root_node = None

    def _init_file(self):
        """Get the file handle open for writing"""
        self.file = open(self.path, "w")

    def write(self, point, value):
        """
        Write out an individual point (unimplemented).

        :param point: location associated with the data to be written
        :type point: should be a shapes.Site object
            Note(LB): Some subclasses override this behavior in their
            write() methods. Be careful.

        :param value: some value to be written to the file
        :type value: determined by concrete class implementation
        """
        raise NotImplementedError

    def close(self):
        """Close and flush the file. Send finished messages."""
        self.file.close()

    def serialize(self, iterable):
        """Wrapper for writing all items in an iterable object."""
        if isinstance(iterable, dict):
            iterable = iterable.items()
        for key, val in iterable:
            self.write(key, val)
        self.close()


class XMLFileWriter(FileWriter):
    """
    Base class for writing XML files.
    """

    def write_header(self):
        """
        Write out the file header.
        """
        raise NotImplementedError

    def write_footer(self):
        """
        Write out the file footer.
        """
        raise NotImplementedError

    def serialize(self, iterable):
        """
        Wrapper for writing all items in an iterable object.
        """
        if isinstance(iterable, dict):
            iterable = iterable.items()
        self.write_header()
        for key, val in iterable:
            self.write(key, val)
        self.write_footer()
        self.close()


class DBWriter(object):
    """
    Abstact class implementing the "serialize" interface to output an iterable
    to the database.

    Subclasses must either implement get_output_type() and insert_datum() or
    override serialize().
    """

    def __init__(self, session, nrml_path, oq_job_id):
        self.nrml_path = nrml_path
        self.oq_job_id = oq_job_id
        self.session = session
        self.output = None

    def insert_output(self, output_type):
        assert self.output is None
        """Insert an `uiapi.output` record for the job at hand."""
        LOGGER.info("> insert_output")
        job = self.session.query(OqJob).filter(
            OqJob.id == self.oq_job_id).one()
        self.output = Output(owner=job.owner, oq_job=job,
                             display_name=basename(self.nrml_path),
                             output_type=output_type, db_backed=True)
        self.session.add(self.output)
        LOGGER.info("output = '%s'" % self.output)
        LOGGER.info("< insert_output")

    def insert_datum(self, key, values):
        """
        Called for each item of the iterable during serialize.
        """
        raise NotImplementedError()

    def serialize(self, iterable):
        """
        Implementation of the "serialize" interface.

        An Output record with type get_output_type() will be created, then
        each item of the iterable will be serialized in turn to the database.
        """
        LOGGER.info("> serialize")
        LOGGER.info("serializing %s points" % len(iterable))

        if not self.output:
            self.insert_output(self.get_output_type())
        LOGGER.info("output = '%s'" % self.output)

        for key, values in iterable:
            self.insert_datum(key, values)

        self.session.commit()

        LOGGER.info("serialized %s points" % len(iterable))
        LOGGER.info("< serialize")
