# -*- coding: utf-8 -*-

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


"""
Set up some system-wide loggers
TODO(jmc): init_logs should take filename, or sysout
TODO(jmc): support debug level per logger.

"""
import logging
import socket
import threading

import kombu
from openquake.signalling import AMQPMessageConsumer, amqp_connect

logging.basicConfig()

LEVELS = {'debug': logging.DEBUG,
          'info': logging.INFO,
          'warn': logging.WARNING,
          'error': logging.ERROR,
          'critical': logging.CRITICAL}

LOG = logging.getLogger()
HAZARD_LOG = logging.getLogger('hazard')


def init_logs_amqp_send(level, job_id):
    """
    Initialize logs to send records with level `level` or above from loggers
    'oq.job.*' through AMQP.

    Adds handler :class:`AMQPHandler` to logger 'oq.job'.
    """
    amqp_handlers = [h for h in logging.root.handlers
                     if isinstance(h, AMQPHandler)]

    if amqp_handlers:
        [handler] = amqp_handlers
        handler.set_job_id(job_id)
        return

    logging.getLogger("amqplib").propagate = False
    set_logger_level(logging.root, level)
    hdlr = AMQPHandler()
    hdlr.set_job_id(job_id)
    logging.root.addHandler(hdlr)


def set_logger_level(logger, level):
    """
    Apply symbolic name of level `level` to logger `logger`.

    Uses mapping :const:`LEVELS`.
    """
    logger.setLevel(LEVELS.get(level, logging.WARNING))


class AMQPHandler(logging.Handler):  # pylint: disable=R0902
    """
    Logging handler that sends log messages to AMQP.

    Transmitted log records are represented as json-encoded dictionaries
    with values of LogRecord object enclosed. Those values should be enough
    to reconstruct LogRecord upon receiving.

    :param level: minimum logging level to be sent.
    """

    #: Routing key for a record is generated by formatting the record
    #: with this format. All the same keys as for usual log records
    #: are available, but very few make sense being in routing key.
    ROUTING_KEY_FORMAT = "oq.job.%(job_id)s.%(name)s"

    _MDC = threading.local()

    # pylint: disable=R0913
    def __init__(self, level=logging.NOTSET):
        logging.Handler.__init__(self, level=level)
        self.producer = self._initialize()

    @staticmethod
    def _initialize():
        """Initialize amqp artefacts."""
        _, channel, exchange = amqp_connect()
        return kombu.messaging.Producer(channel, exchange, serializer='json')

    def set_job_id(self, job_id):
        """
        Set the job id for handler.

        Is called from :func:`init_logs_amqp_send`. Provided job id
        will be added to log records (see :meth:`emit`).
        """
        self._MDC.job_id = job_id

    def emit(self, record):
        # exc_info objects are not easily serializable
        # so we can not support "logger.exception()"
        assert not record.exc_info
        data = vars(record).copy()
        # instead of 'msg' with placeholders putting formatted message
        # and removing args list to guarantee serializability no matter
        # what was in args
        data['msg'] = record.getMessage()
        data['args'] = ()
        data['hostname'] = socket.getfqdn()
        data['job_id'] = getattr(self._MDC, 'job_id', None)

        routing_key = self.ROUTING_KEY_FORMAT % data
        self.producer.publish(data, routing_key)


class AMQPLogSource(AMQPMessageConsumer):
    """
    Receiving part of logging-over-AMQP solution.

    Works in pair with :class:`AMQPHandler`: receives its log messages
    with respect to provided routing key -- logger name. Relogs all received
    log records.
    """
    def message_callback(self, record_data, msg):
        """
        Create log record and handle it.

        Never stops :meth:`consumers's execution
        <openquake.signalling.AMQPMessageConsumer.run>`.
        """
        record = object.__new__(logging.LogRecord)
        record.__dict__.update(record_data)
        logger = logging.getLogger(record.name)
        if logger.isEnabledFor(record.levelno):
            logger.handle(record)
