# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0.  If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Copyright 1997 - July 2008 CWI, August 2008 - 2016 MonetDB B.V.

import logging
import tempfile
import re
import pickle
import pdb

from pymonetdb.sql import monetize, pythonize
from pymonetdb.exceptions import ProgrammingError, InterfaceError
from pymonetdb import mapi
from six import u, PY2

logger = logging.getLogger("pymonetdb")


class Cursor(object):
    """This object represents a database cursor, which is used to manage
    the context of a fetch operation. Cursors created from the same
    connection are not isolated, i.e., any changes done to the
    database by a cursor are immediately visible by the other
    cursors"""

    def __init__(self, connection):
        """This read-only attribute return a reference to the Connection
        object on which the cursor was created."""
        self.connection = connection

        # last executed operation (query)
        self.operation = ""

        # This read/write attribute specifies the number of rows to
        # fetch at a time with .fetchmany()
        self.arraysize = connection.replysize

        # This read-only attribute specifies the number of rows that
        # the last .execute*() produced (for DQL statements like
        # 'select') or affected (for DML statements like 'update' or
        # 'insert').
        #
        # The attribute is -1 in case no .execute*() has been
        # performed on the cursor or the rowcount of the last
        # operation is cannot be determined by the interface.
        self.rowcount = -1

        # This read-only attribute is a sequence of 7-item
        # sequences.
        #
        # Each of these sequences contains information describing
        # one result column:
        #
        #   (name,
        #    type_code,
        #    display_size,
        #    internal_size,
        #    precision,
        #    scale,
        #    null_ok)
        #
        # This attribute will be None for operations that
        # do not return rows or if the cursor has not had an
        # operation invoked via the .execute*() method yet.
        self.description = None

        # This read-only attribute indicates at which row
        # we currently are
        self.rownumber = -1

        self.__executed = None

        # the offset of the current resultset in the total resultset
        self.__offset = 0

        # the resultset
        self.__rows = []

        # used to identify a query during server contact.
        # Only select queries have query ID
        self.__query_id = -1

        # This is a Python list object to which the interface appends
        # tuples (exception class, exception value) for all messages
        # which the interfaces receives from the underlying database for
        # this cursor.
        #
        # The list is cleared by all standard cursor methods calls (prior
        # to executing the call) except for the .fetch*() calls
        # automatically to avoid excessive memory usage and can also be
        # cleared by executing "del cursor.messages[:]".
        #
        # All error and warning messages generated by the database are
        # placed into this list, so checking the list allows the user to
        # verify correct operation of the method calls.
        self.messages = []

        # This read-only attribute provides the rowid of the last
        # modified row (most databases return a rowid only when a single
        # INSERT operation is performed). If the operation does not set
        # a rowid or if the database does not support rowids, this
        # attribute should be set to None.
        #
        # The semantics of .lastrowid are undefined in case the last
        # executed statement modified more than one row, e.g. when
        # using INSERT with .executemany().
        self.lastrowid = None

    def __check_executed(self):
        if not self.__executed:
            self.__exception_handler(ProgrammingError, "do a execute() first")

    def close(self):
        """ Close the cursor now (rather than whenever __del__ is
        called).  The cursor will be unusable from this point
        forward; an Error (or subclass) exception will be raised
        if any operation is attempted with the cursor."""
        self.connection = None

    def execute(self, operation, parameters=None):
        """Prepare and execute a database operation (query or
        command).  Parameters may be provided as mapping and
        will be bound to variables in the operation.
        """

        if not self.connection:
            self.__exception_handler(ProgrammingError, "cursor is closed")

        # clear message history
        self.messages = []

        # convert to utf-8
        if PY2:
            if type(operation) == unicode:
                # don't decode if it is already unicode
                operation = operation.encode('utf-8')
            else:
                operation = u(operation).encode('utf-8')

        # set the number of rows to fetch
        if self.arraysize != self.connection.replysize:
            self.connection.set_replysize(self.arraysize)

        if operation == self.operation:
            # same operation, DBAPI mentioned something about reuse
            # but monetdb doesn't support this
            pass
        else:
            self.operation = operation

        query = ""
        if parameters:
            if isinstance(parameters, dict):
                query = operation % dict([(k, monetize.convert(v))
                                          for (k, v) in parameters.items()])
            elif type(parameters) == list or type(parameters) == tuple:
                query = operation % tuple(
                    [monetize.convert(item) for item in parameters])
            elif isinstance(parameters, str):
                query = operation % monetize.convert(parameters)
            else:
                msg = "Parameters should be None, dict or list, now it is %s"
                self.__exception_handler(ValueError, msg % type(parameters))
        else:
            query = operation

        block = self.connection.execute(query)
        self.__store_result(block)
        self.rownumber = 0
        self.__executed = operation
        return self.rowcount

    def executemany(self, operation, seq_of_parameters):
        """Prepare a database operation (query or command) and then
        execute it against all parameter sequences or mappings
        found in the sequence seq_of_parameters.

        It will return the number or rows affected
        """

        count = 0
        for parameters in seq_of_parameters:
            count += self.execute(operation, parameters)
        self.rowcount = count
        return count

    def __exportparameters(self, ftype, fname, query, quantity_parameters,
                           sample):
        """ Exports the input parameters of a given UDF execution
            to the Python process. Used internally for .debug() and
            .export() functions.
        """

        # create a dummy function that only exports its parameters
        # using the pickle module
        if ftype == 5:
            # table producing function
            return_type = "TABLE(s STRING)"
        else:
            return_type = "STRING"
        if sample == -1:
            export_function = """
                CREATE OR REPLACE FUNCTION export_parameters(*)
                RETURNS %s LANGUAGE PYTHON
                {
                    import inspect
                    import pickle
                    frame = inspect.currentframe();
                    args, _, _, values = inspect.getargvalues(frame);
                    dd = {x: values[x] for x in args};
                    del dd['_conn']
                    return pickle.dumps(dd);
                };""" % return_type
        else:
            export_function = """
                CREATE OR REPLACE FUNCTION export_parameters(*)
                RETURNS %s LANGUAGE PYTHON
                {
                import inspect
                import pickle
                import numpy
                frame = inspect.currentframe();
                args, _, _, values = inspect.getargvalues(frame);
                dd = {x: values[x] for x in args};
                del dd['_conn']
                result = dict()
                argname = "arg1"
                x = numpy.arange(len(dd[argname]))
                x = numpy.random.choice(x,%s,replace=False)
                for i in range(len(dd)-2):
                    argname = "arg" + str(i + 1)
                    result = dd[argname]
                    aux = []
                    for j in range(len(x)):
                        aux.append(result[x[j]])
                    dd[argname] = aux
                    print(dd[argname])
                print(x)
                return pickle.dumps(dd);
                };
                """ % (return_type, str(sample))

        if fname not in query:
            raise Exception("Function %s not found in query!" % fname)

        query = query.replace(fname, 'export_parameters')
        query = query.replace(';', ' sample 1;')

        self.execute(export_function)
        self.execute(query)
        input_data = self.fetchall()
        self.execute('DROP FUNCTION export_parameters;')
        if len(input_data) <= 0:
            raise Exception("Could not load input data!")
        arguments = pickle.loads(str(input_data[0][0]))

        if len(arguments) != quantity_parameters + 2:
            raise Exception("Incorrect amount of input arguments found!")

        return arguments

    def export(self, query, fname, sample=-1, filespath='./'):
        """ Exports a Python UDF and its input parameters to a given
            file so it can be called locally in an IDE environment.
        """

        # first retrieve UDF information from the server
        self.execute("""
            SELECT func,type
            FROM functions
            WHERE language >= 6 AND language <= 11 AND name='%s';""" % fname)
        data = self.fetchall()
        self.execute("""
            SELECT args.name
            FROM args INNER JOIN functions ON args.func_id=functions.id
            WHERE functions.name='%s' AND args.inout=1
            ORDER BY args.number;""" % fname)
        input_names = self.fetchall()
        quantity_parameters = len(input_names)
        fcode = data[0][0]
        ftype = data[0][1]
        parameter_list = []
        # exporting Python UDF Function
        if len(data) == 0:
            raise Exception("Function not found!")
        else:
            parameters = '('
            for x in range(0, len(input_names)):
                parameter = str(input_names[x]).split('\'')
                if x < len(input_names) - 1:
                    parameter_list.append(parameter[1])
                    parameters = parameters + parameter[1] + ','
                else:
                    parameter_list.append(parameter[1])
                    parameters = parameters + parameter[1] + '): \n'

            data = str(data[0]).replace('\\t', '\t').split('\\n')

            python_udf = 'import pickle \n \n \ndef ' + fname + parameters
            for x in range(1, len(data) - 1):
                python_udf = python_udf + '\t' + str(data[x]) + '\n'

        # exporting Columns as Binary Files
        arguments = self.__exportparameters(ftype, fname, query,
                                            quantity_parameters, sample)
        result = dict()
        for i in range(len(arguments) - 2):
            argname = "arg%d" % (i + 1)
            result[parameter_list[i]] = arguments[argname]
        pickle.dump(result, open(filespath + 'input_data.bin', 'wb'))

        # loading Columns in Pyhton & Call Function
        python_udf += '\n' + 'input_parameters = pickle.load(open(\'' + filespath + 'input_data.bin\',\'rb\'))' + '\n' + fname + '('
        for i in range(0, quantity_parameters):
            if i < quantity_parameters - 1:
                python_udf += 'input_parameters[\'' + parameter_list[i] + '\'],'
            else:
                python_udf += 'input_parameters[\'' + parameter_list[i] + '\'])'

        file = open(filespath + fname + '.py', 'w')
        file.write(python_udf)
        file.close()

    def debug(self, query, fname, sample=-1):
        """ Locally debug a given Python UDF function in a SQL query
            using the PDB debugger. Optionally can run on only a
            sample of the input data, for faster data export.
        """

        # first gather information about the function
        self.execute("""
            SELECT func, type
            FROM functions
            WHERE language>=6 AND language <= 11 AND name='%s';""" % fname)
        data = self.fetchall()
        if len(data) == 0:
            raise Exception("Function not found!")

        # then gather the input arguments of the function
        self.execute("""
            SELECT args.name, args.type
            FROM args
            INNER JOIN functions ON args.func_id=functions.id
            WHERE functions.name='%s' AND args.inout=1
            ORDER BY args.number;""" % fname)
        input_types = self.fetchall()

        fcode = data[0][0]
        ftype = data[0][1]

        # now obtain the input columns
        arguments = self.__exportparameters(ftype, fname, query,
                                            len(input_types), sample)

        arglist = "_columns, _column_types, _conn"
        cleaned_arguments = dict()
        for i in range(len(input_types)):
            argname = "arg%d" % (i + 1)
            if argname not in arguments:
                raise Exception("Argument %d not found!" % (i + 1))
            input_name = str(input_types[i][0])
            cleaned_arguments[input_name] = arguments[argname]
            arglist += ", %s" % input_name
        cleaned_arguments['_columns'] = arguments['_columns']
        cleaned_arguments['_column_types'] = arguments['_column_types']

        # create a temporary file for the function execution and run it
        with tempfile.NamedTemporaryFile() as f:
            fcode = fcode.strip()
            fcode = re.sub('^{', '', fcode)
            fcode = re.sub('};$', '', fcode)
            fcode = re.sub('^\n', '', fcode)
            function_definition = "def pyfun(%s):\n %s\n" % (
                arglist, fcode.replace("\n", "\n "))
            f.write(function_definition)
            f.flush()
            execfile(f.name, globals(), locals())

            class LoopbackObject(object):
                def __init__(self, connection):
                    self.__conn = connection

                def execute(self, query):
                    self.__conn.execute("""
                        CREATE OR REPLACE FUNCTION export_parameters(*)
                        RETURNS TABLE(s STRING) LANGUAGE PYTHON
                        {
                            import inspect
                            import pickle
                            frame = inspect.currentframe();
                            args, _, _, values = inspect.getargvalues(frame);
                            dd = {x: values[x] for x in args};
                            del dd['_conn']
                            del dd['_columns']
                            del dd['_column_types']
                            return pickle.dumps(dd);
                        };""")
                    self.__conn.execute("""
                        SELECT *
                        FROM (%s) AS xx
                        LIMIT 1""" % query)
                    query_description = self.__conn.description
                    self.__conn.execute("""
                        SELECT *
                        FROM export_parameters ( (%s) );""" % query)
                    data = self.__conn.fetchall()
                    arguments = pickle.loads(str(data[0][0]))
                    self.__conn.execute('DROP FUNCTION export_parameters;')
                    if len(arguments) != len(query_description):
                        raise Exception("Incorrect number of parameters!")
                    result = dict()
                    for i in range(len(arguments)):
                        argname = "arg%d" % (i + 1)
                        result[query_description[i][0]] = arguments[argname]
                    return result

            cleaned_arguments['_conn'] = LoopbackObject(self)
            pdb.set_trace()
            return locals()['pyfun'](*[], **cleaned_arguments)

    def fetchone(self):
        """Fetch the next row of a query result set, returning a
        single sequence, or None when no more data is available."""

        self.__check_executed()

        if self.__query_id == -1:
            msg = "query didn't result in a resultset"
            self.__exception_handler(ProgrammingError, msg)

        if self.rownumber >= self.rowcount:
            return None

        if self.rownumber >= (self.__offset + len(self.__rows)):
            self.nextset()

        result = self.__rows[self.rownumber - self.__offset]
        self.rownumber += 1
        return result

    def fetchmany(self, size=None):
        """Fetch the next set of rows of a query result, returning a
        sequence of sequences (e.g. a list of tuples). An empty
        sequence is returned when no more rows are available.

        The number of rows to fetch per call is specified by the
        parameter.  If it is not given, the cursor's arraysize
        determines the number of rows to be fetched. The method
        should try to fetch as many rows as indicated by the size
        parameter. If this is not possible due to the specified
        number of rows not being available, fewer rows may be
        returned.

        An Error (or subclass) exception is raised if the previous
        call to .execute*() did not produce any result set or no
        call was issued yet.

        Note there are performance considerations involved with
        the size parameter.  For optimal performance, it is
        usually best to use the arraysize attribute.  If the size
        parameter is used, then it is best for it to retain the
        same value from one .fetchmany() call to the next."""

        self.__check_executed()

        if self.rownumber >= self.rowcount:
            return []

        end = self.rownumber + (size or self.arraysize)
        end = min(end, self.rowcount)
        result = self.__rows[self.rownumber - self.__offset:
                             end - self.__offset]
        self.rownumber = min(end, len(self.__rows) + self.__offset)

        while (end > self.rownumber) and self.nextset():
            result += self.__rows[self.rownumber - self.__offset:
                                  end - self.__offset]
            self.rownumber = min(end, len(self.__rows) + self.__offset)
        return result

    def fetchall(self):
        """Fetch all (remaining) rows of a query result, returning
        them as a sequence of sequences (e.g. a list of tuples).
        Note that the cursor's arraysize attribute can affect the
        performance of this operation.

        An Error (or subclass) exception is raised if the previous
        call to .execute*() did not produce any result set or no
        call was issued yet."""

        self.__check_executed()

        if self.__query_id == -1:
            msg = "query didn't result in a resultset"
            self.__exception_handler(ProgrammingError, msg)

        result = self.__rows[self.rownumber - self.__offset:]
        self.rownumber = len(self.__rows) + self.__offset

        # slide the window over the resultset
        while self.nextset():
            result += self.__rows
            self.rownumber = len(self.__rows) + self.__offset

        return result

    def nextset(self):
        """This method will make the cursor skip to the next
        available set, discarding any remaining rows from the
        current set.

        If there are no more sets, the method returns
        None. Otherwise, it returns a true value and subsequent
        calls to the fetch methods will return rows from the next
        result set.

        An Error (or subclass) exception is raised if the previous
        call to .execute*() did not produce any result set or no
        call was issued yet."""

        self.__check_executed()

        if self.rownumber >= self.rowcount:
            return False

        self.__offset += len(self.__rows)

        end = min(self.rowcount, self.rownumber + self.arraysize)
        amount = end - self.__offset

        command = 'Xexport %s %s %s' % (self.__query_id, self.__offset, amount)
        block = self.connection.command(command)
        self.__store_result(block)
        return True

    def setinputsizes(self, sizes):
        """
        This method would be used before the .execute*() method
        is invoked to reserve memory. This implementation doesn't
        use this.
        """
        pass

    def setoutputsize(self, size, column=None):
        """
        Set a column buffer size for fetches of large columns
        This implementation doesn't use this
        """
        pass

    def __iter__(self):
        return self

    def next(self):
        row = self.fetchone()
        if not row:
            raise StopIteration
        return row

    def __next__(self):
        return self.next()

    def __store_result(self, block):
        """ parses the mapi result into a resultset"""

        if not block:
            block = ""

        column_name = ""
        scale = display_size = internal_size = precision = 0
        null_ok = False
        type_ = []

        for line in block.split("\n"):
            if line.startswith(mapi.MSG_INFO):
                logger.info(line[1:])
                self.messages.append((Warning, line[1:]))

            elif line.startswith(mapi.MSG_QTABLE):
                (self.__query_id, rowcount, columns,
                 tuples) = line[2:].split()[:4]

                columns = int(columns)  # number of columns in result
                self.rowcount = int(rowcount)  # total number of rows
                # tuples = int(tuples)     # number of rows in this set
                self.__rows = []

                # set up fields for description
                # table_name = [None] * columns
                column_name = [None] * columns
                type_ = [None] * columns
                display_size = [None] * columns
                internal_size = [None] * columns
                precision = [None] * columns
                scale = [None] * columns
                null_ok = [None] * columns
                # typesizes = [(0, 0)] * columns
                self.__offset = 0
                self.lastrowid = None

            elif line.startswith(mapi.MSG_HEADER):
                (data, identity) = line[1:].split("#")
                values = [x.strip() for x in data.split(",")]
                identity = identity.strip()

                if identity == "name":
                    column_name = values
                # elif identity == "table_name":
                #    table_name = values
                elif identity == "type":
                    type_ = values
                # elif identity == "length":
                #   length = values
                elif identity == "typesizes":
                    typesizes = [[int(j) for j in i.split()] for i in values]
                    internal_size = [x[0] for x in typesizes]
                    for num, typeelem in enumerate(type_):
                        if typeelem in ['decimal']:
                            precision[num] = typesizes[num][0]
                            scale[num] = typesizes[num][1]
                # else:
                #    msg = "unknown header field"
                #    self.messages.append((InterfaceError, msg))
                #    self.__exception_handler(InterfaceError, msg)

                self.description = list(
                    zip(column_name, type_, display_size, internal_size,
                        precision, scale, null_ok))
                self.__offset = 0
                self.lastrowid = None

            elif line.startswith(mapi.MSG_TUPLE):
                values = self.__parse_tuple(line)
                self.__rows.append(values)

            elif line.startswith(mapi.MSG_TUPLE_NOSLICE):
                self.__rows.append((line[1:], ))

            elif line.startswith(mapi.MSG_QBLOCK):
                self.__rows = []

            elif line.startswith(mapi.MSG_QSCHEMA):
                self.__offset = 0
                self.lastrowid = None
                self.__rows = []
                self.description = None
                self.rowcount = -1

            elif line.startswith(mapi.MSG_QUPDATE):
                (affected, identity) = line[2:].split()[:2]
                self.__offset = 0
                self.__rows = []
                self.description = None
                self.rowcount = int(affected)
                self.lastrowid = int(identity)
                self.__query_id = -1

            elif line.startswith(mapi.MSG_QTRANS):
                self.__offset = 0
                self.lastrowid = None
                self.__rows = []
                self.description = None
                self.rowcount = -1

            elif line == mapi.MSG_PROMPT:
                return

            elif line.startswith(mapi.MSG_ERROR):
                self.__exception_handler(ProgrammingError, line[1:])

        self.__exception_handler(InterfaceError, "Unknown state, %s" % block)

    def __parse_tuple(self, line):
        """ parses a mapi data tuple, and returns a list of python types"""
        elements = line[1:-1].split(',\t')
        if len(elements) == len(self.description):
            return tuple([
                pythonize.convert(element.strip(), description[1])
                for (element, description) in zip(elements, self.description)
            ])
        else:
            self.__exception_handler(InterfaceError,
                                     "length of row doesn't match header")

    def scroll(self, value, mode='relative'):
        """Scroll the cursor in the result set to a new position according
        to mode.

        If mode is 'relative' (default), value is taken as offset to
        the current position in the result set, if set to 'absolute',
        value states an absolute target position.

        An IndexError is raised in case a scroll operation would
        leave the result set.
        """
        self.__check_executed()

        if mode not in ['relative', 'absolute']:
            msg = "unknown mode '%s'" % mode
            self.__exception_handler(ProgrammingError, msg)

        if mode == 'relative':
            value += self.rownumber

        if value > self.rowcount:
            self.__exception_handler(IndexError,
                                     "value beyond length of resultset")

        self.__offset = value
        end = min(self.rowcount, self.rownumber + self.arraysize)
        amount = end - self.__offset
        command = 'Xexport %s %s %s' % (self.__query_id, self.__offset, amount)
        block = self.connection.command(command)
        self.__store_result(block)

    def __exception_handler(self, exception_class, message):
        """ raises the exception specified by exception, and add the error
        to the message list """
        self.messages.append((exception_class, message))
        raise exception_class(message)
