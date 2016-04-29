from datetime import datetime, date, time, timedelta
from decimal import Decimal, localcontext
import struct
import six
import re
import uuid

from six.moves import reduce

from .tds_base import *
from .collate import ucs2_codec, Collation, lcid2charset, raw_collation
from . import tz


_flt4_struct = struct.Struct('f')
_flt8_struct = struct.Struct('d')
_utc = tz.utc


def _applytz(dt, tz):
    if not tz:
        return dt
    dt = dt.replace(tzinfo=tz)
    return dt


def _decode_num(buf):
    """ Decodes little-endian integer from buffer

    Buffer can be of any size
    """
    return reduce(lambda acc, val: acc * 256 + ord(val), reversed(buf), 0)


class PlpReader(object):
    """ Partially length prefixed reader

    Spec: http://msdn.microsoft.com/en-us/library/dd340469.aspx
    """
    def __init__(self, r):
        """
        :param r: An instance of :class:`_TdsReader`
        """
        self._rdr = r
        size = r.get_uint8()
        self._size = size

    def is_null(self):
        """
        :return: True if stored value is NULL
        """
        return self._size == PLP_NULL

    def is_unknown_len(self):
        """
        :return: True if total size is unknown upfront
        """
        return self._size == PLP_UNKNOWN

    def size(self):
        """
        :return: Total size in bytes if is_uknown_len and is_null are both False
        """
        return self._size

    def chunks(self):
        """ Generates chunks from stream, each chunk is an instace of bytes.
        """
        if self.is_null():
            return
        total = 0
        while True:
            chunk_len = self._rdr.get_uint()
            if chunk_len == 0:
                if not self.is_unknown_len() and total != self._size:
                    msg = "PLP actual length (%d) doesn't match reported length (%d)" % (total, self._size)
                    self._rdr.session.bad_stream(msg)

                return

            total += chunk_len
            left = chunk_len
            while left:
                buf = self._rdr.read(left)
                yield buf
                left -= len(buf)


class SqlTypeMetaclass(CommonEqualityMixin):
    def __repr__(self):
        return '<sqltype:{}>'.format(self.get_declaration())

    def get_declaration(self):
        raise NotImplementedError()


class ImageType(SqlTypeMetaclass):
    def get_declaration(self):
        return 'IMAGE'


class BinaryType(SqlTypeMetaclass):
    def __init__(self, size=30):
        self._size = size

    @property
    def size(self):
        return self._size

    def get_declaration(self):
        return 'BINARY({})'.format(self._size)


class VarBinaryType(SqlTypeMetaclass):
    def __init__(self, size=30):
        self._size = size

    @property
    def size(self):
        return self._size

    def get_declaration(self):
        return 'VARBINARY({})'.format(self._size)


class VarBinaryMaxType(SqlTypeMetaclass):
    def get_declaration(self):
        return 'VARBINARY(MAX)'


class CharType(SqlTypeMetaclass):
    def __init__(self, size=30):
        self._size = size

    @property
    def size(self):
        return self._size

    def get_declaration(self):
        return 'CHAR({})'.format(self._size)


class VarCharType(SqlTypeMetaclass):
    def __init__(self, size=30):
        self._size = size

    @property
    def size(self):
        return self._size

    def get_declaration(self):
        return 'VARCHAR({})'.format(self._size)


class VarCharMaxType(SqlTypeMetaclass):
    def get_declaration(self):
        return 'VARCHAR(MAX)'


class NCharType(SqlTypeMetaclass):
    def __init__(self, size=30):
        self._size = size

    @property
    def size(self):
        return self._size

    def get_declaration(self):
        return 'NCHAR({})'.format(self._size)


class NVarCharType(SqlTypeMetaclass):
    def __init__(self, size=30):
        self._size = size

    @property
    def size(self):
        return self._size

    def get_declaration(self):
        return 'NVARCHAR({})'.format(self._size)


class NVarCharMaxType(SqlTypeMetaclass):
    def get_declaration(self):
        return 'NVARCHAR(MAX)'


class TextType(SqlTypeMetaclass):
    def get_declaration(self):
        return 'TEXT'


class NTextType(SqlTypeMetaclass):
    def get_declaration(self):
        return 'NTEXT'


class XmlType(SqlTypeMetaclass):
    def get_declaration(self):
        return 'XML'


class SmallMoneyType(SqlTypeMetaclass):
    def get_declaration(self):
        return 'SMALLMONEY'


class MoneyType(SqlTypeMetaclass):
    def get_declaration(self):
        return 'MONEY'


class DecimalType(SqlTypeMetaclass):
    def __init__(self, precision=18, scale=0):
        self._precision = precision
        self._scale = scale

    @classmethod
    def from_value(cls, value):
        if not (-10 ** 38 + 1 <= value <= 10 ** 38 - 1):
            raise DataError('Decimal value is out of range')
        value = value.normalize()
        _, digits, exp = value.as_tuple()
        if exp > 0:
            scale = 0
            prec = len(digits) + exp
        else:
            scale = -exp
            prec = max(len(digits), scale)
        return cls(precision=prec, scale=scale)

    @property
    def precision(self):
        return self._precision

    @property
    def scale(self):
        return self._scale

    def get_declaration(self):
        return 'DECIMAL({}, {})'.format(self._precision, self._scale)


class UniqueIdentifierType(SqlTypeMetaclass):
    def get_declaration(self):
        return 'UNIQUEIDENTIFIER'


class VariantType(SqlTypeMetaclass):
    def get_declaration(self):
        return 'SQL_VARIANT'


class SqlValueMetaclass(CommonEqualityMixin):
    pass


class BaseTypeSerializer(CommonEqualityMixin):
    """ Base type for TDS data types.

    All TDS types should derive from it.
    In addition actual types should provide the following:

    - type - class variable storing type identifier
    """
    def get_typeid(self):
        """ Returns type identifier of type. """
        return self.type

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        """ Class method that parses declaration and returns a type instance.

        :param declaration: type declaration string
        :param nullable: true if type have to be nullable, false otherwise
        :param connection: instance of :class:`_TdsSocket`
        :return: If declaration is parsed, returns type instance,
                 otherwise returns None.

        Should be implemented in actual types.
        """
        raise NotImplementedError

    @classmethod
    def from_stream(cls, r):
        """ Class method that reads and returns a type instance.

        :param r: An instance of :class:`_TdsReader` to read type from.

        Should be implemented in actual types.
        """
        raise NotImplementedError

    def write_info(self, w):
        """ Writes type info into w stream.

        :param w: An instance of :class:`_TdsWriter` to write into.

        Should be symmetrical to from_stream method.
        Should be implemented in actual types.
        """
        raise NotImplementedError

    def write(self, w, value):
        """ Writes type's value into stream

        :param w: An instance of :class:`_TdsWriter` to write into.
        :param value: A value to be stored, should be compatible with the type

        Should be implemented in actual types.
        """
        raise NotImplementedError

    def read(self, r):
        """ Reads value from the stream.

        :param r: An instance of :class:`_TdsReader` to read value from.
        :return: A read value.

        Should be implemented in actual types.
        """
        raise NotImplementedError


class BasePrimitiveTypeSerializer(BaseTypeSerializer):
    """ Base type for primitive TDS data types.

    Primitive type is a fixed size type with no type arguments.
    All primitive TDS types should derive from it.
    In addition actual types should provide the following:

    - type - class variable storing type identifier
    - declaration - class variable storing name of sql type
    - isntance - class variable storing instance of class
    """

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        if not nullable and declaration == cls.declaration:
            return cls.instance

    @classmethod
    def from_stream(cls, r):
        return cls.instance

    def write_info(self, w):
        pass


class BaseTypeSerializerN(BaseTypeSerializer):
    """ Base type for nullable TDS data types.

    All nullable TDS types should derive from it.
    In addition actual types should provide the following:

    - type - class variable storing type identifier
    - subtypes - class variable storing dict {subtype_size: subtype_instance}
    """

    def __init__(self, size):
        assert size in self.subtypes
        self._size = size
        self._current_subtype = self.subtypes[size]

    def get_typeid(self):
        return self._current_subtype.get_typeid()

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        if nullable:
            for size, subtype in cls.subtypes.items():
                inst = subtype.from_declaration(declaration, False, connection)
                if inst:
                    return cls(size)

    @classmethod
    def from_stream(cls, r):
        size = r.get_byte()
        if size not in cls.subtypes:
            raise InterfaceError('Invalid %s size' % cls.type, size)
        return cls(size)

    def write_info(self, w):
        w.put_byte(self._size)

    def read(self, r):
        size = r.get_byte()
        if size == 0:
            return None
        if size not in self.subtypes:
            raise r.session.bad_stream('Invalid %s size' % self.type, size)
        return self.subtypes[size].read(r)

    def write(self, w, val):
        if val is None:
            w.put_byte(0)
            return
        w.put_byte(self._size)
        self._current_subtype.write(w, val)


class BitType(SqlTypeMetaclass):
    type = SYBBITN

    def get_declaration(self):
        return 'BIT'


class TinyIntType(SqlTypeMetaclass):
    type = SYBINTN
    size = 1

    def get_declaration(self):
        return 'TINYINT'


class SmallIntType(SqlTypeMetaclass):
    type = SYBINTN
    size = 2

    def get_declaration(self):
        return 'SMALLINT'


class IntType(SqlTypeMetaclass):
    type = SYBINTN
    size = 4

    def get_declaration(self):
        return 'INT'


class BigIntType(SqlTypeMetaclass):
    type = SYBINTN
    size = 8

    def get_declaration(self):
        return 'BIGINT'


class RealType(SqlTypeMetaclass):
    def get_declaration(self):
        return 'REAL'


class FloatType(SqlTypeMetaclass):
    def get_declaration(self):
        return 'FLOAT'


class BitSerializer(BasePrimitiveTypeSerializer):
    type = SYBBIT
    declaration = 'BIT'

    def write(self, w, value):
        w.put_byte(1 if value else 0)

    def read(self, r):
        return bool(r.get_byte())

BitSerializer.instance = BitSerializer()


class BitNSerializer(BaseTypeSerializerN):
    type = SYBBITN
    subtypes = {1 : BitSerializer.instance}

    def __init__(self, typ):
        super(BitNSerializer, self).__init__(size=1)
        self._typ = typ

    def __repr__(self):
        return 'BitNSerializer({})'.format(self._typ)


BitNSerializer.instance = BitNSerializer(BitType())


class TinyIntSerializer(BasePrimitiveTypeSerializer):
    type = SYBINT1
    declaration = 'TINYINT'

    def write(self, w, val):
        w.put_byte(val)

    def read(self, r):
        return r.get_byte()

TinyIntSerializer.instance = TinyIntSerializer()


class SmallIntSerializer(BasePrimitiveTypeSerializer):
    type = SYBINT2
    declaration = 'SMALLINT'

    def write(self, w, val):
        w.put_smallint(val)

    def read(self, r):
        return r.get_smallint()

SmallIntSerializer.instance = SmallIntSerializer()


class IntSerializer(BasePrimitiveTypeSerializer):
    type = SYBINT4
    declaration = 'INT'

    def write(self, w, val):
        w.put_int(val)

    def read(self, r):
        return r.get_int()

IntSerializer.instance = IntSerializer()


class BigIntSerializer(BasePrimitiveTypeSerializer):
    type = SYBINT8
    declaration = 'BIGINT'

    def write(self, w, val):
        w.put_int8(val)

    def read(self, r):
        return r.get_int8()

BigIntSerializer.instance = BigIntSerializer()


class IntNSerializer(BaseTypeSerializerN):
    type = SYBINTN

    subtypes = {
        1: TinyIntSerializer.instance,
        2: SmallIntSerializer.instance,
        4: IntSerializer.instance,
        8: BigIntSerializer.instance,
    }

    type_by_size = {
        1: TinyIntType(),
        2: SmallIntType(),
        4: IntType(),
        8: BigIntType(),
    }

    def __init__(self, typ):
        super(IntNSerializer, self).__init__(size=typ.size)
        self._typ = typ

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        if nullable:
            for size, subtype in cls.subtypes.items():
                inst = subtype.from_declaration(declaration, False, connection)
                if inst:
                    return cls(cls.type_by_size[size])

    @classmethod
    def from_stream(cls, r):
        size = r.get_byte()
        if size not in cls.subtypes:
            raise InterfaceError('Invalid %s size' % cls.type, size)
        return cls(cls.type_by_size[size])

    def __repr__(self):
        return 'IntN({})'.format(self._size)


class RealSerializer(BasePrimitiveTypeSerializer):
    type = SYBREAL
    declaration = 'REAL'

    def write(self, w, val):
        w.pack(_flt4_struct, val)

    def read(self, r):
        return r.unpack(_flt4_struct)[0]

RealSerializer.instance = RealSerializer()


class FloatSerializer(BasePrimitiveTypeSerializer):
    type = SYBFLT8
    declaration = 'FLOAT'

    def write(self, w, val):
        w.pack(_flt8_struct, val)

    def read(self, r):
        return r.unpack(_flt8_struct)[0]

FloatSerializer.instance = FloatSerializer()


class FloatNSerializer(BaseTypeSerializerN):
    type = SYBFLTN

    subtypes = {
        4: RealSerializer.instance,
        8: FloatSerializer.instance,
    }


class VarChar(SqlValueMetaclass):
    def __init__(self, val, collation=raw_collation):
        self._val = val
        self._collation = collation

    @property
    def collation(self):
        return self._collation

    @property
    def val(self):
        return self._val

    def __str__(self):
        return self._val


class VarChar70Serializer(BaseTypeSerializer):
    type = XSYBVARCHAR

    def __init__(self, size, collation=raw_collation, codec=None):
        #if size <= 0 or size > 8000:
        #    raise DataError('Invalid size for VARCHAR field')
        self._size = size
        self._collation = collation
        if codec:
            self._codec = codec
        else:
            self._codec = collation.get_codec()

    @classmethod
    def from_stream(cls, r):
        size = r.get_smallint()
        return cls(size, codec=r._session.conn.server_codec)

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        m = re.match(r'VARCHAR\((\d+)\)', declaration)
        if m:
            return cls(int(m.group(1)), connection.server_codec)

    def write_info(self, w):
        w.put_smallint(self._size)
        #w.put_smallint(self._size)

    def write(self, w, val):
        if val is None:
            w.put_smallint(-1)
        else:
            val = force_unicode(val)
            val, _ = self._codec.encode(val)
            w.put_smallint(len(val))
            #w.put_smallint(len(val))
            w.write(val)

    def read(self, r):
        size = r.get_smallint()
        if size < 0:
            return None
        return r.read_str(size, self._codec)


class VarChar71Serializer(VarChar70Serializer):
    @classmethod
    def from_stream(cls, r):
        size = r.get_smallint()
        collation = r.get_collation()
        return cls(size, collation)

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        m = re.match(r'VARCHAR\((\d+)\)', declaration)
        if m:
            return cls(int(m.group(1)), connection.collation)

    def write_info(self, w):
        super(VarChar71Serializer, self).write_info(w)
        w.put_collation(self._collation)


class VarChar72Serializer(VarChar71Serializer):
    @classmethod
    def from_stream(cls, r):
        size = r.get_usmallint()
        collation = r.get_collation()
        if size == 0xffff:
            return VarCharMaxSerializer(collation)
        return cls(size, collation)

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        if declaration == 'VARCHAR(MAX)':
            return VarCharMaxSerializer(connection.collation)
        m = re.match(r'VARCHAR\((\d+)\)', declaration)
        if m:
            return cls(int(m.group(1)), connection.collation)


class VarCharMaxSerializer(VarChar72Serializer):
    def __init__(self, collation=raw_collation):
        super(VarChar72Serializer, self).__init__(0, collation)

    def write_info(self, w):
        w.put_usmallint(PLP_MARKER)
        w.put_collation(self._collation)

    def write(self, w, val):
        if val is None:
            w.put_uint8(PLP_NULL)
        else:
            val = force_unicode(val)
            val, _ = self._codec.encode(val)
            w.put_int8(len(val))
            if len(val) > 0:
                w.put_int(len(val))
                w.write(val)
            w.put_int(0)

    def read(self, r):
        r = PlpReader(r)
        if r.is_null():
            return None
        return ''.join(iterdecode(r.chunks(), self._codec))


class NVarChar70Serializer(BaseTypeSerializer):
    type = XSYBNVARCHAR

    def __init__(self, size, collation=raw_collation):
        #if size <= 0 or size > 4000:
        #    raise DataError('Invalid size for NVARCHAR field')
        self._size = size
        self._collation = collation

    @classmethod
    def from_stream(cls, r):
        size = r.get_usmallint()
        return cls(size / 2)

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        m = re.match(r'NVARCHAR\((\d+)\)', declaration)
        if m:
            return cls(int(m.group(1)))

    def write_info(self, w):
        w.put_usmallint(self._size * 2)
        #w.put_smallint(self._size)

    def write(self, w, val):
        if val is None:
            w.put_usmallint(0xffff)
        else:
            if isinstance(val, bytes):
                val = force_unicode(val)
            buf, _ = ucs2_codec.encode(val)
            l = len(buf)
            w.put_usmallint(l)
            w.write(buf)

    def read(self, r):
        size = r.get_usmallint()
        if size == 0xffff:
            return None
        return r.read_str(size, ucs2_codec)


class NVarChar71Serializer(NVarChar70Serializer):
    @classmethod
    def from_stream(cls, r):
        size = r.get_usmallint()
        collation = r.get_collation()
        return cls(size / 2, collation)

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        m = re.match(r'NVARCHAR\((\d+)\)', declaration)
        if m:
            return cls(int(m.group(1)), connection.collation)

    def write_info(self, w):
        super(NVarChar71Serializer, self).write_info(w)
        w.put_collation(self._collation)


class NVarChar72Serializer(NVarChar71Serializer):
    @classmethod
    def from_stream(cls, r):
        size = r.get_usmallint()
        collation = r.get_collation()
        if size == 0xffff:
            return NVarCharMaxSerializer(collation=collation)
        return cls(size / 2, collation=collation)

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        if declaration == 'NVARCHAR(MAX)':
            return NVarCharMaxSerializer(collation=connection.collation)
        m = re.match(r'NVARCHAR\((\d+)\)', declaration)
        if m:
            return cls(int(m.group(1)), connection.collation)


class NVarCharMaxSerializer(NVarChar72Serializer):
    def __init__(self, collation=raw_collation):
        super(NVarCharMaxSerializer, self).__init__(size=-1, collation=collation)

    def __repr__(self):
        return 'NVarCharMax(s={},c={})'.format(self._size, repr(self._collation))

    def get_typeid(self):
        return SYBNTEXT

    def write_info(self, w):
        w.put_usmallint(PLP_MARKER)
        w.put_collation(self._collation)

    def write(self, w, val):
        if val is None:
            w.put_uint8(PLP_NULL)
        else:
            if isinstance(val, bytes):
                val = force_unicode(val)
            val, _ = ucs2_codec.encode(val)
            w.put_uint8(len(val))
            if len(val) > 0:
                w.put_uint(len(val))
                w.write(val)
            w.put_uint(0)

    def read(self, r):
        r = PlpReader(r)
        if r.is_null():
            return None
        res = ''.join(iterdecode(r.chunks(), ucs2_codec))
        return res


class XmlSerializer(NVarCharMaxSerializer):
    type = SYBMSXML
    declaration = 'XML'

    def __init__(self, schema={}):
        super(XmlSerializer, self).__init__(0)
        self._schema = schema

    def __repr__(self):
        return 'XmlSerializer(schema={})'.format(repr(self._schema))

    def get_typeid(self):
        return self.type

    @classmethod
    def from_stream(cls, r):
        has_schema = r.get_byte()
        schema = {}
        if has_schema:
            schema['dbname'] = r.read_ucs2(r.get_byte())
            schema['owner'] = r.read_ucs2(r.get_byte())
            schema['collection'] = r.read_ucs2(r.get_smallint())
        return cls(schema)

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        if declaration == cls.declaration:
            return cls()

    def write_info(self, w):
        if self._schema:
            w.put_byte(1)
            w.put_byte(len(self._schema['dbname']))
            w.write_ucs2(self._schema['dbname'])
            w.put_byte(len(self._schema['owner']))
            w.write_ucs2(self._schema['owner'])
            w.put_usmallint(len(self._schema['collection']))
            w.write_ucs2(self._schema['collection'])
        else:
            w.put_byte(0)


class Text70Serializer(BaseTypeSerializer):
    type = SYBTEXT
    declaration = 'TEXT'

    def __init__(self, size=0, table_name='', collation=raw_collation, codec=None):
        self._size = size
        self._table_name = table_name
        self._collation = collation
        if codec:
            self._codec = codec
        else:
            self._codec = collation.get_codec()

    def __repr__(self):
        return 'Text70(size={},table_name={},codec={})'.format(self._size, self._table_name, self._codec)

    @classmethod
    def from_stream(cls, r):
        size = r.get_int()
        table_name = r.read_ucs2(r.get_smallint())
        return cls(size, table_name, codec=r.session.conn.server_codec)

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        if declaration == cls.declaration:
            return cls()

    def write_info(self, w):
        w.put_int(self._size)

    def write(self, w, val):
        if val is None:
            w.put_int(-1)
        else:
            val = force_unicode(val)
            val, _ = self._codec.encode(val)
            w.put_int(len(val))
            w.write(val)

    def read(self, r):
        size = r.get_byte()
        if size == 0:
            return None
        readall(r, size)  # textptr
        readall(r, 8)  # timestamp
        colsize = r.get_int()
        return r.read_str(colsize, self._codec)


class Text71Serializer(Text70Serializer):
    def __repr__(self):
        return 'Text71(size={}, table_name={}, collation={})'.format(
            self._size, self._table_name, repr(self._collation)
        )

    @classmethod
    def from_stream(cls, r):
        size = r.get_int()
        collation = r.get_collation()
        table_name = r.read_ucs2(r.get_smallint())
        return cls(size, table_name, collation)

    def write_info(self, w):
        w.put_int(self._size)
        w.put_collation(self._collation)


class Text72Serializer(Text71Serializer):
    def __init__(self, size=0, table_name_parts=(), collation=raw_collation):
        super(Text72Serializer, self).__init__(size=size, table_name='.'.join(table_name_parts), collation=collation)
        self._table_name_parts = table_name_parts

    @classmethod
    def from_stream(cls, r):
        size = r.get_int()
        collation = r.get_collation()
        num_parts = r.get_byte()
        parts = []
        for _ in range(num_parts):
            parts.append(r.read_ucs2(r.get_smallint()))
        return cls(size, parts, collation)


class NText70Serializer(BaseTypeSerializer):
    type = SYBNTEXT
    declaration = 'NTEXT'

    def __init__(self, size=0, table_name='', collation=raw_collation):
        self._size = size
        self._collation = collation
        self._table_name = table_name

    def __repr__(self):
        return 'NText70(size={}, table_name={})'.format(self._size, self._table_name)

    @classmethod
    def from_stream(cls, r):
        size = r.get_int()
        table_name = r.read_ucs2(r.get_smallint())
        return cls(size, table_name)

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        if declaration == cls.declaration:
            return cls()

    def read(self, r):
        textptr_size = r.get_byte()
        if textptr_size == 0:
            return None
        readall(r, textptr_size)  # textptr
        readall(r, 8)  # timestamp
        colsize = r.get_int()
        return r.read_str(colsize, ucs2_codec)

    def write_info(self, w):
        w.put_int(self._size * 2)

    def write(self, w, val):
        if val is None:
            w.put_int(-1)
        else:
            w.put_int(len(val) * 2)
            w.write_ucs2(val)


class NText71Serializer(NText70Serializer):
    def __repr__(self):
        return 'NText71(size={}, table_name={}, collation={})'.format(self._size,
                                                                      self._table_name,
                                                                      repr(self._collation))

    @classmethod
    def from_stream(cls, r):
        size = r.get_int()
        collation = r.get_collation()
        table_name = r.read_ucs2(r.get_smallint())
        return cls(size, table_name, collation)

    def write_info(self, w):
        w.put_int(self._size)
        w.put_collation(self._collation)

    def read(self, r):
        textptr_size = r.get_byte()
        if textptr_size == 0:
            return None
        readall(r, textptr_size)  # textptr
        readall(r, 8)  # timestamp
        colsize = r.get_int()
        return r.read_str(colsize, ucs2_codec)


class NText72Serializer(NText71Serializer):
    def __init__(self, size=0, table_name_parts=(), collation=raw_collation):
        super(NText72Serializer, self).__init__(size=size, collation=collation)
        self._table_name_parts = table_name_parts

    def __repr__(self):
        return 'NText72Serializer(s={},table_name={},coll={})'.format(
            self._size, self._table_name_parts, self._collation)

    @classmethod
    def from_stream(cls, r):
        size = r.get_int()
        collation = r.get_collation()
        num_parts = r.get_byte()
        parts = []
        for _ in range(num_parts):
            parts.append(r.read_ucs2(r.get_smallint()))
        return cls(size, parts, collation)


class Binary(bytes, SqlValueMetaclass):
    def __repr__(self):
        return 'Binary({0})'.format(super(Binary, self).__repr__())


class VarBinarySerializer(BaseTypeSerializer):
    type = XSYBVARBINARY

    def __init__(self, size):
        self._size = size

    def __repr__(self):
        return 'VarBinary({})'.format(self._size)

    @classmethod
    def from_stream(cls, r):
        size = r.get_usmallint()
        return cls(size)

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        m = re.match(r'VARBINARY\((\d+)\)', declaration)
        if m:
            return cls(int(m.group(1)))

    def write_info(self, w):
        w.put_usmallint(self._size)

    def write(self, w, val):
        if val is None:
            w.put_usmallint(0xffff)
        else:
            w.put_usmallint(len(val))
            w.write(val)

    def read(self, r):
        size = r.get_usmallint()
        if size == 0xffff:
            return None
        return readall(r, size)


class VarBinarySerializer72(VarBinarySerializer):
    def __repr__(self):
        return 'VarBinary72({})'.format(self._size)

    @classmethod
    def from_stream(cls, r):
        size = r.get_usmallint()
        if size == 0xffff:
            return VarBinarySerializerMax()
        return cls(size)

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        if declaration == 'VARBINARY(MAX)':
            return VarBinarySerializerMax()
        m = re.match(r'VARBINARY\((\d+)\)', declaration)
        if m:
            return cls(int(m.group(1)))


class VarBinarySerializerMax(VarBinarySerializer):
    def __init__(self):
        super(VarBinarySerializerMax, self).__init__(0)

    def __repr__(self):
        return 'VarBinaryMax()'

    def write_info(self, w):
        w.put_usmallint(PLP_MARKER)

    def write(self, w, val):
        if val is None:
            w.put_uint8(PLP_NULL)
        else:
            w.put_uint8(len(val))
            if val:
                w.put_uint(len(val))
                w.write(val)
            w.put_uint(0)

    def read(self, r):
        r = PlpReader(r)
        if r.is_null():
            return None
        return b''.join(r.chunks())


class Image70Serializer(BaseTypeSerializer):
    type = SYBIMAGE
    declaration = 'IMAGE'

    def __init__(self, size=0, table_name=''):
        self._table_name = table_name
        self._size = size

    def __repr__(self):
        return 'Image70(tn={},s={})'.format(repr(self._table_name), self._size)

    @classmethod
    def from_stream(cls, r):
        size = r.get_int()
        table_name = r.read_ucs2(r.get_smallint())
        return cls(size, table_name)

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        if declaration == cls.declaration:
            return cls()

    def read(self, r):
        size = r.get_byte()
        if size == 16:  # Jeff's hack
            readall(r, 16)  # textptr
            readall(r, 8)  # timestamp
            colsize = r.get_int()
            return readall(r, colsize)
        else:
            return None

    def write(self, w, val):
        if val is None:
            w.put_int(-1)
            return
        w.put_int(len(val))
        w.write(val)

    def write_info(self, w):
        w.put_int(self._size)


class Image72Serializer(Image70Serializer):
    def __init__(self, size=0, parts=[]):
        self._parts = parts
        self._size = size

    def __repr__(self):
        return 'Image72(p={},s={})'.format(self._parts, self._size)

    @classmethod
    def from_stream(cls, r):
        size = r.get_int()
        num_parts = r.get_byte()
        parts = []
        for _ in range(num_parts):
            parts.append(r.read_ucs2(r.get_usmallint()))
        return Image72Serializer(size, parts)


_datetime_base_date = datetime(1900, 1, 1)


class SmallDateTimeType(SqlTypeMetaclass):
    def get_declaration(self):
        return 'SMALLDATETIME'


class DateTimeType(SqlTypeMetaclass):
    def get_declaration(self):
        return 'DATETIME'


class SmallDateTime(SqlValueMetaclass):
    """Corresponds to MSSQL smalldatetime"""
    def __init__(self, days, minutes):
        """

        @param days: Days since 1900-01-01
        @param minutes: Minutes since 00:00:00
        """
        self._days = days
        self._minutes = minutes

    @property
    def days(self):
        return self._days

    @property
    def minutes(self):
        return self._minutes

    def to_pydatetime(self):
        return _datetime_base_date + timedelta(days=self._days, minutes=self._minutes)

    @classmethod
    def from_pydatetime(cls, dt):
        days = (dt - _datetime_base_date).days
        minutes = dt.hour * 60 + dt.minute
        return cls(days=days, minutes=minutes)


class BaseDateTimeSerializer(BaseTypeSerializer):
    pass


class SmallDateTimeSerializer(BasePrimitiveTypeSerializer, BaseDateTimeSerializer):
    type = SYBDATETIME4
    declaration = 'SMALLDATETIME'

    _struct = struct.Struct('<HH')

    def write(self, w, val):
        if val.tzinfo:
            if not w.session.use_tz:
                raise DataError('Timezone-aware datetime is used without specifying use_tz')
            val = val.astimezone(w.session.use_tz).replace(tzinfo=None)
        dt = SmallDateTime.from_pydatetime(val)
        w.pack(self._struct, dt.days, dt.minutes)

    def read(self, r):
        days, minutes = r.unpack(self._struct)
        dt = SmallDateTime(days=days, minutes=minutes)
        tzinfo = None
        if r.session.tzinfo_factory is not None:
            tzinfo = r.session.tzinfo_factory(0)
        return dt.to_pydatetime().replace(tzinfo=tzinfo)

SmallDateTimeSerializer.instance = SmallDateTimeSerializer()


class DateTime(SqlValueMetaclass):
    """Corresponds to MSSQL datetime"""
    MIN_PYDATETIME = datetime(1753, 1, 1, 0, 0, 0)
    MAX_PYDATETIME = datetime(9999, 12, 31, 23, 59, 59, 997000)

    def __init__(self, days, time_part):
        """

        @param days: Days since 1900-01-01
        @param time_part: Number of 1/300 of seconds since 00:00:00
        """
        self._days = days
        self._time_part = time_part

    @property
    def days(self):
        return self._days

    @property
    def time_part(self):
        return self._time_part

    def to_pydatetime(self):
        ms = int(round(self._time_part % 300 * 10 / 3.0))
        secs = self._time_part // 300
        return _datetime_base_date + timedelta(days=self._days, seconds=secs, milliseconds=ms)

    @classmethod
    def from_pydatetime(cls, dt):
        if not (cls.MIN_PYDATETIME <= dt <= cls.MAX_PYDATETIME):
            raise DataError('Datetime is out of range')
        days = (dt - _datetime_base_date).days
        ms = dt.microsecond // 1000
        tm = (dt.hour * 60 * 60 + dt.minute * 60 + dt.second) * 300 + int(round(ms * 3 / 10.0))
        return cls(days=days, time_part=tm)


class DateTimeSerializer(BasePrimitiveTypeSerializer, BaseDateTimeSerializer):
    type = SYBDATETIME
    declaration = 'DATETIME'

    _struct = struct.Struct('<ll')

    def write(self, w, val):
        if val.tzinfo:
            if not w.session.use_tz:
                raise DataError('Timezone-aware datetime is used without specifying use_tz')
            val = val.astimezone(w.session.use_tz).replace(tzinfo=None)
        w.write(self.encode(val))

    def read(self, r):
        days, t = r.unpack(self._struct)
        tzinfo = None
        if r.session.tzinfo_factory is not None:
            tzinfo = r.session.tzinfo_factory(0)
        return _applytz(self.decode(days, t), tzinfo)

    @classmethod
    def encode(cls, value):
        #cls.validate(value)
        if type(value) == date:
            value = datetime.combine(value, time(0, 0, 0))
        dt = DateTime.from_pydatetime(value)
        return cls._struct.pack(dt.days, dt.time_part)

    @classmethod
    def decode(cls, days, time_part):
        dt = DateTime(days=days, time_part=time_part)
        return dt.to_pydatetime()

DateTimeSerializer.instance = DateTimeSerializer()


class DateTimeNSerializer(BaseTypeSerializerN, BaseDateTimeSerializer):
    type = SYBDATETIMN
    subtypes = {
        4: SmallDateTimeSerializer.instance,
        8: DateTimeSerializer.instance,
    }


_datetime2_base_date = datetime(1, 1, 1)


class DateType(SqlTypeMetaclass):
    type = SYBMSDATE

    def get_declaration(self):
        return "DATE"


class Date(SqlValueMetaclass):
    MIN_PYDATE = date(1, 1, 1)
    MAX_PYDATE = date(9999, 12, 31)

    def __init__(self, days):
        """
        Creates sql date object
        @param days: Days since 0001-01-01
        """
        self._days = days

    @property
    def days(self):
        return self._days

    def to_pydate(self):
        """
        Converts sql date to Python date
        @return: Python date
        """
        return (_datetime2_base_date + timedelta(days=self._days)).date()

    @classmethod
    def from_pydate(cls, pydate):
        """
        Creates sql date object from Python date object.
        @param value: Python date
        @return: sql date
        """
        return cls(days=(datetime.combine(pydate, time(0, 0, 0)) - _datetime2_base_date).days)


class TimeType(SqlTypeMetaclass):
    type = SYBMSTIME

    def __init__(self, precision=7):
        self._precision = precision

    @property
    def precision(self):
        return self._precision

    def get_declaration(self):
        return 'TIME({0})'.format(self.precision)


class Time(SqlValueMetaclass):
    def __init__(self, nsec):
        """
        Creates sql time object.
        Maximum precision which sql server supports is 100 nanoseconds.
        Values more precise than 100 nanoseconds will be truncated.
        @param nsec: Nanoseconds from 00:00:00
        """
        self._nsec = nsec

    @property
    def nsec(self):
        return self._nsec

    def to_pytime(self):
        """
        Converts sql time object into Python's time object
        this will truncate nanoseconds to microseconds
        @return: naive time
        """
        nanoseconds = self._nsec
        hours = nanoseconds // 1000000000 // 60 // 60
        nanoseconds -= hours * 60 * 60 * 1000000000
        minutes = nanoseconds // 1000000000 // 60
        nanoseconds -= minutes * 60 * 1000000000
        seconds = nanoseconds // 1000000000
        nanoseconds -= seconds * 1000000000
        return time(hours, minutes, seconds, nanoseconds // 1000)

    @classmethod
    def from_pytime(cls, pytime):
        """
        Converts Python time object to sql time object
        ignoring timezone
        @param pytime: Python time object
        @return: sql time object
        """
        secs = pytime.hour * 60 * 60 + pytime.minute * 60 + pytime.second
        nsec = secs * 10 ** 9 + pytime.microsecond * 1000
        return cls(nsec=nsec)


class DateTime2Type(SqlTypeMetaclass):
    type = SYBMSDATETIME2

    def __init__(self, precision=7):
        self._precision = precision

    @property
    def precision(self):
        return self._precision

    def get_declaration(self):
        return 'DATETIME2({0})'.format(self.precision)


class DateTime2(SqlValueMetaclass):
    type = SYBMSDATETIME2

    def __init__(self, date, time):
        """
        Creates datetime2 object
        @param date: sql date object
        @param time: sql time object
        """
        self._date = date
        self._time = time

    @property
    def date(self):
        return self._date

    @property
    def time(self):
        return self._time

    def to_pydatetime(self):
        """
        Converts datetime2 object into Python's datetime.datetime object
        @return: naive datetime.datetime
        """
        return datetime.combine(self._date.to_pydate(), self._time.to_pytime())

    @classmethod
    def from_pydatetime(cls, pydatetime):
        """
        Creates sql datetime2 object from Python datetime object
        ignoring timezone
        @param pydatetime: Python datetime object
        @return: sql datetime2 object
        """
        return cls(date=Date.from_pydate(pydatetime.date),
                   time=Time.from_pytime(pydatetime.time))


class DateTimeOffsetType(SqlTypeMetaclass):
    type = SYBMSDATETIMEOFFSET

    def __init__(self, precision=7):
        self._precision = precision

    @property
    def precision(self):
        return self._precision

    def get_declaration(self):
        return 'DATETIMEOFFSET({0})'.format(self.precision)


class DateTimeOffset(SqlValueMetaclass):
    def __init__(self, date, time, offset):
        """
        Creates datetime2 object
        @param date: sql date object in UTC
        @param time: sql time object in UTC
        @param offset: time zone offset in minutes
        """
        self._date = date
        self._time = time
        self._offset = offset

    def to_pydatetime(self):
        """
        Converts datetimeoffset object into Python's datetime.datetime object
        @return: time zone aware datetime.datetime
        """
        dt = datetime.combine(self._date.to_pydate(), self._time.to_pytime())
        from .tz import FixedOffsetTimezone
        return dt.replace(tzinfo=_utc).astimezone(FixedOffsetTimezone(self._offset))


class BaseDateTime73Serializer(BaseTypeSerializer):
    _precision_to_len = {
        0: 3,
        1: 3,
        2: 3,
        3: 4,
        4: 4,
        5: 5,
        6: 5,
        7: 5,
    }

    def _write_time(self, w, t, prec):
        val = t.nsec // (10 ** (9 - prec))
        w.write(struct.pack('<Q', val)[:self._precision_to_len[prec]])

    def _read_time(self, r, size, prec):
        time_buf = readall(r, size)
        val = _decode_num(time_buf)
        val *= 10 ** (7 - prec)
        nanoseconds = val * 100
        return Time(nsec=nanoseconds)

    def _write_date(self, w, value):
        days = value.days
        buf = struct.pack('<l', days)[:3]
        w.write(buf)

    def _read_date(self, r):
        days = _decode_num(readall(r, 3))
        return Date(days=days)


class MsDateSerializer(BasePrimitiveTypeSerializer, BaseDateTime73Serializer):
    type = SYBMSDATE
    declaration = 'DATE'

    def __init__(self, typ):
        self._typ = typ

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        if declaration == cls.declaration:
            return cls(DateType())

    @classmethod
    def from_stream(cls, r):
        return cls(DateType())

    def write(self, w, value):
        if value is None:
            w.put_byte(0)
        else:
            w.put_byte(3)
            self._write_date(w, Date.from_pydate(value))

    def read_fixed(self, r):
        return self._read_date(r).to_pydate()

    def read(self, r):
        size = r.get_byte()
        if size == 0:
            return None
        return self._read_date(r).to_pydate()


class MsTimeSerializer(BaseDateTime73Serializer):
    type = SYBMSTIME

    def __init__(self, typ):
        self._typ = typ
        self._size = self._precision_to_len[typ.precision]

    @classmethod
    def read_type(cls, r):
        prec = r.get_byte()
        return TimeType(precision=prec)

    @classmethod
    def from_stream(cls, r):
        return cls(cls.read_type(r))

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        m = re.match(r'TIME\((\d+)\)', declaration)
        if m:
            return cls(TimeType(precision=int(m.group(1))))

    def write_info(self, w):
        w.put_byte(self._typ.precision)

    def write(self, w, value):
        if value is None:
            w.put_byte(0)
        else:
            if value.tzinfo:
                if not w.session.use_tz:
                    raise DataError('Timezone-aware datetime is used without specifying use_tz')
                value = value.astimezone(w.session.use_tz).replace(tzinfo=None)
            w.put_byte(self._size)
            self._write_time(w, Time.from_pytime(value), self._typ.precision)

    def read_fixed(self, r, size):
        res = self._read_time(r, size, self._typ.precision).to_pytime()
        if r.session.tzinfo_factory is not None:
            tzinfo = r.session.tzinfo_factory(0)
            res = res.replace(tzinfo=tzinfo)
        return res

    def read(self, r):
        size = r.get_byte()
        if size == 0:
            return None
        return self.read_fixed(r, size)


class DateTime2Serializer(BaseDateTime73Serializer):
    type = SYBMSDATETIME2

    def __init__(self, typ):
        self._typ = typ
        self._size = self._precision_to_len[typ.precision] + 3

    @classmethod
    def from_stream(cls, r):
        prec = r.get_byte()
        return cls(DateTime2Type(precision=prec))

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        if declaration == 'DATETIME2':
            return cls(DateTime2Type())
        m = re.match(r'DATETIME2\((\d+)\)', declaration)
        if m:
            return cls(DateTime2Type(precision=int(m.group(1))))

    def write_info(self, w):
        w.put_byte(self._typ.precision)

    def write(self, w, value):
        if value is None:
            w.put_byte(0)
        else:
            if value.tzinfo:
                if not w.session.use_tz:
                    raise DataError('Timezone-aware datetime is used without specifying use_tz')
                value = value.astimezone(w.session.use_tz).replace(tzinfo=None)
            w.put_byte(self._size)
            self._write_time(w, Time.from_pytime(value), self._typ.precision)
            self._write_date(w, Date.from_pydate(value))

    def read_fixed(self, r, size):
        time = self._read_time(r, size - 3, self._typ.precision)
        date = self._read_date(r)
        dt = DateTime2(date=date, time=time)
        res = dt.to_pydatetime()
        if r.session.tzinfo_factory is not None:
            tzinfo = r.session.tzinfo_factory(0)
            res = res.replace(tzinfo=tzinfo)
        return res

    def read(self, r):
        size = r.get_byte()
        if size == 0:
            return None
        return self.read_fixed(r, size)


class DateTimeOffsetSerializer(BaseDateTime73Serializer):
    type = SYBMSDATETIMEOFFSET

    def __init__(self, typ):
        self._typ = typ
        self._size = self._precision_to_len[typ.precision] + 5

    @classmethod
    def from_stream(cls, r):
        prec = r.get_byte()
        return cls(DateTimeOffsetType(precision=prec))

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        if declaration == 'DATETIMEOFFSET':
            return cls(DateTimeOffsetType())
        m = re.match(r'DATETIMEOFFSET\((\d+)\)', declaration)
        if m:
            return cls(DateTimeOffsetType(precision=int(m.group(1))))

    def write_info(self, w):
        w.put_byte(self._typ.precision)

    def write(self, w, value):
        if value is None:
            w.put_byte(0)
        else:
            utcoffset = value.utcoffset()
            value = value.astimezone(_utc).replace(tzinfo=None)

            w.put_byte(self._size)
            self._write_time(w, Time.from_pytime(value), self._typ.precision)
            self._write_date(w, Date.from_pydate(value))
            w.put_smallint(int(total_seconds(utcoffset)) // 60)

    def read_fixed(self, r, size):
        time = self._read_time(r, size - 5, self._typ.precision)
        date = self._read_date(r)
        offset = r.get_smallint()
        dt = DateTimeOffset(date=date, time=time, offset=offset)
        return dt.to_pydatetime()

    def read(self, r):
        size = r.get_byte()
        if size == 0:
            return None
        return self.read_fixed(r, size)


class MsDecimalSerializer(BaseTypeSerializer):
    type = SYBDECIMAL

    _max_size = 17

    _bytes_per_prec = [
        #
        # precision can't be 0 but using a value > 0 assure no
        # core if for some bug it's 0...
        #
        1,
        5, 5, 5, 5, 5, 5, 5, 5, 5,
        9, 9, 9, 9, 9, 9, 9, 9, 9, 9,
        13, 13, 13, 13, 13, 13, 13, 13, 13,
        17, 17, 17, 17, 17, 17, 17, 17, 17, 17,
    ]

    _info_struct = struct.Struct('BBB')

    @property
    def scale(self):
        return self._scale

    @property
    def precision(self):
        return self._prec

    def __init__(self, scale=0, prec=18):
        if prec > 38:
            raise DataError('Precision of decimal value is out of range')
        self._scale = scale
        self._prec = prec
        self._size = self._bytes_per_prec[prec]

    def __repr__(self):
        return 'MsDecimal(scale={}, prec={})'.format(self._scale, self._prec)

    @classmethod
    def from_value(cls, value):
        sql_type = DecimalType.from_value(value)
        return cls(scale=sql_type.scale, prec=sql_type.precision)

    @classmethod
    def from_stream(cls, r):
        size, prec, scale = r.unpack(cls._info_struct)
        return cls(scale=scale, prec=prec)

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        if declaration == 'DECIMAL':
            return cls()
        m = re.match(r'DECIMAL\((\d+),\s*(\d+)\)', declaration)
        if m:
            return cls(int(m.group(2)), int(m.group(1)))

    def write_info(self, w):
        w.pack(self._info_struct, self._size, self._prec, self._scale)

    def write(self, w, value):
        if value is None:
            w.put_byte(0)
            return
        if not isinstance(value, Decimal):
            value = Decimal(value)
        value = value.normalize()
        scale = self._scale
        size = self._size
        w.put_byte(size)
        val = value
        positive = 1 if val > 0 else 0
        w.put_byte(positive)  # sign
        with localcontext() as ctx:
            ctx.prec = 38
            if not positive:
                val *= -1
            size -= 1
            val = val * (10 ** scale)
        for i in range(size):
            w.put_byte(int(val % 256))
            val //= 256
        assert val == 0

    def _decode(self, positive, buf):
        val = _decode_num(buf)
        val = Decimal(val)
        with localcontext() as ctx:
            ctx.prec = 38
            if not positive:
                val *= -1
            val /= 10 ** self._scale
        return val

    def read_fixed(self, r, size):
        positive = r.get_byte()
        buf = readall(r, size - 1)
        return self._decode(positive, buf)

    def read(self, r):
        size = r.get_byte()
        if size <= 0:
            return None
        return self.read_fixed(r, size)


class Money4Serializer(BasePrimitiveTypeSerializer):
    type = SYBMONEY4
    declaration = 'SMALLMONEY'

    def read(self, r):
        return Decimal(r.get_int()) / 10000

    def write(self, w, val):
        val = int(val * 10000)
        w.put_int(val)

Money4Serializer.instance = Money4Serializer()


class Money8Serializer(BasePrimitiveTypeSerializer):
    type = SYBMONEY
    declaration = 'MONEY'

    _struct = struct.Struct('<lL')

    def read(self, r):
        hi, lo = r.unpack(self._struct)
        val = hi * (2 ** 32) + lo
        return Decimal(val) / 10000

    def write(self, w, val):
        val = val * 10000
        hi = int(val // (2 ** 32))
        lo = int(val % (2 ** 32))
        w.pack(self._struct, hi, lo)

Money8Serializer.instance = Money8Serializer()


class MoneyNSerializer(BaseTypeSerializerN):
    type = SYBMONEYN

    subtypes = {
        4: Money4Serializer.instance,
        8: Money8Serializer.instance,
    }


class MsUniqueSerializer(BaseTypeSerializer):
    type = SYBUNIQUE
    declaration = 'UNIQUEIDENTIFIER'

    def __repr__(self):
        return 'MsUniqueSerializer()'

    @classmethod
    def from_stream(cls, r):
        size = r.get_byte()
        if size != 16:
            raise InterfaceError('Invalid size of UNIQUEIDENTIFIER field')
        return cls.instance

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        if declaration == cls.declaration:
            return cls.instance

    def write_info(self, w):
        w.put_byte(16)

    def write(self, w, value):
        if value is None:
            w.put_byte(0)
        else:
            w.put_byte(16)
            w.write(value.bytes_le)

    def read_fixed(self, r, size):
        return uuid.UUID(bytes_le=readall(r, size))

    def read(self, r):
        size = r.get_byte()
        if size == 0:
            return None
        if size != 16:
            raise InterfaceError('Invalid size of UNIQUEIDENTIFIER field')
        return self.read_fixed(r, size)
MsUniqueSerializer.instance = MsUniqueSerializer()


def _variant_read_str(r, size):
    collation = r.get_collation()
    r.get_usmallint()
    return r.read_str(size, collation.get_codec())


def _variant_read_nstr(r, size):
    r.get_collation()
    r.get_usmallint()
    return r.read_str(size, ucs2_codec)


def _variant_read_decimal(r, size):
    prec, scale = r.unpack(VariantSerializer._decimal_info_struct)
    return MsDecimalSerializer(prec=prec, scale=scale).read_fixed(r, size)


def _variant_read_binary(r, size):
    r.get_usmallint()
    return readall(r, size)


class VariantSerializer(BaseTypeSerializer):
    type = SYBVARIANT
    declaration = 'SQL_VARIANT'

    _decimal_info_struct = struct.Struct('BB')

    _type_map = {
        GUIDTYPE: lambda r, size: MsUniqueSerializer.instance.read_fixed(r, size),
        BITTYPE: lambda r, size: BitSerializer.instance.read(r),
        INT1TYPE: lambda r, size: TinyIntSerializer.instance.read(r),
        INT2TYPE: lambda r, size: SmallIntSerializer.instance.read(r),
        INT4TYPE: lambda r, size: IntSerializer.instance.read(r),
        INT8TYPE: lambda r, size: BigIntSerializer.instance.read(r),
        DATETIMETYPE: lambda r, size: DateTimeSerializer.instance.read(r),
        DATETIM4TYPE: lambda r, size: SmallDateTimeSerializer.instance.read(r),
        FLT4TYPE: lambda r, size: RealSerializer.instance.read(r),
        FLT8TYPE: lambda r, size: FloatSerializer.instance.read(r),
        MONEYTYPE: lambda r, size: Money8Serializer.instance.read(r),
        MONEY4TYPE: lambda r, size: Money4Serializer.instance.read(r),
        DATENTYPE: lambda r, size: MsDateSerializer(DateType()).read_fixed(r),

        TIMENTYPE: lambda r, size: MsTimeSerializer(TimeType(precision=r.get_byte())).read_fixed(r, size),
        DATETIME2NTYPE: lambda r, size: DateTime2Serializer(DateTime2Type(precision=r.get_byte())).read_fixed(r, size),
        DATETIMEOFFSETNTYPE: lambda r, size: DateTimeOffsetSerializer(DateTimeOffsetType(precision=r.get_byte())).read_fixed(r, size),

        BIGVARBINTYPE: _variant_read_binary,
        BIGBINARYTYPE: _variant_read_binary,

        NUMERICNTYPE: _variant_read_decimal,
        DECIMALNTYPE: _variant_read_decimal,

        BIGVARCHRTYPE: _variant_read_str,
        BIGCHARTYPE: _variant_read_str,
        NVARCHARTYPE: _variant_read_nstr,
        NCHARTYPE: _variant_read_nstr,

    }

    def __init__(self, size):
        self._size = size

    @classmethod
    def from_stream(cls, r):
        size = r.get_int()
        return VariantSerializer(size)

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        if declaration == cls.declaration:
            return cls(0)

    def write_info(self, w):
        w.put_int(self._size)

    def read(self, r):
        size = r.get_int()
        if size == 0:
            return None

        type_id = r.get_byte()
        prop_bytes = r.get_byte()
        type_factory = self._type_map.get(type_id)
        if not type_factory:
            r.session.bad_stream('Variant type invalid', type_id)
        return type_factory(r, size - prop_bytes - 2)

    def write(self, w, val):
        if val is None:
            w.put_int(0)
            return
        raise NotImplementedError


class TableType(SqlTypeMetaclass):
    """
    Used to serialize table valued parameters

    spec: https://msdn.microsoft.com/en-us/library/dd304813.aspx
    """
    def __init__(self, typ_schema, typ_name, columns):
        """
        @param typ_schema: Schema where TVP type defined
        @param typ_name: Name of TVP type
        @param columns: List of column types
        """
        if len(typ_schema) > 128:
            raise ValueError("Schema part of TVP name should be no longer than 128 characters")
        if len(typ_name) > 128:
            raise ValueError("Name part of TVP name should be no longer than 128 characters")
        if columns is not None:
            if len(columns) > 1024:
                raise ValueError("TVP cannot have more than 1024 columns")
            if len(columns) < 1:
                raise ValueError("TVP must have at least one column")
        self._typ_dbname = ''  # dbname should always be empty string for TVP according to spec
        self._typ_schema = typ_schema
        self._typ_name = typ_name
        self._columns = columns

    def __repr__(self):
        return 'TableType(s={},n={},cols={})'.format(
            self._typ_schema, self._typ_name, repr(self._columns)
        )

    def get_declaration(self):
        assert not self._typ_dbname
        if self._typ_schema:
            full_name = '{}.{}'.format(self._typ_schema, self._typ_name)
        else:
            full_name = self._typ_name
        return '{} READONLY'.format(full_name)

    @property
    def typ_schema(self):
        return self._typ_schema

    @property
    def typ_name(self):
        return self._typ_name

    @property
    def columns(self):
        return self._columns


class TableValuedParam(SqlValueMetaclass):
    """
    Used to represent a value of table-valued parameter
    """
    def __init__(self, type_name=None, columns=None, rows=None):
        # parsing type name
        self._typ_schema = ''
        self._typ_name = ''
        if type_name:
            parts = type_name.split('.')
            if len(parts) > 2:
                raise ValueError('Type name should consist of at most 2 parts, e.g. dbo.MyType')
            self._typ_name = parts[-1]
            if len(parts) > 1:
                self._typ_schema = parts[0]

        self._columns = columns
        self._rows = rows

    @property
    def typ_name(self):
        return self._typ_name

    @property
    def typ_schema(self):
        return self._typ_schema

    @property
    def columns(self):
        return self._columns

    @property
    def rows(self):
        return self._rows

    def is_null(self):
        return self._rows is None

    def peek_row(self):
        try:
            rows = iter(self._rows)
        except TypeError:
            raise DataError('rows should be iterable')

        try:
            row = next(rows)
        except StopIteration:
            # no rows
            raise DataError("Cannot infer columns from rows for TVP because there are no rows")
        else:
            # put row back
            self._rows = itertools.chain([row], rows)
        return row


class TableSerializer(BaseTypeSerializer):
    """
    Used to serialize table valued parameters

    spec: https://msdn.microsoft.com/en-us/library/dd304813.aspx
    """

    type = TVPTYPE

    def read(self, r):
        """ According to spec TDS does not support output TVP values """
        raise NotImplementedError

    @classmethod
    def from_stream(cls, r):
        """ According to spec TDS does not support output TVP values """
        raise NotImplementedError

    @classmethod
    def from_declaration(cls, declaration, nullable, connection):
        raise NotImplementedError

    def __init__(self, table_type, columns_serializers):
        self._table_type = table_type
        self._columns_serializers = columns_serializers

    def __repr__(self):
        return 'TableSerializer(t={},c={})'.format(
            repr(self._table_type), repr(self._columns_serializers)
        )

    def write_info(self, w):
        """
        Writes TVP_TYPENAME structure

        spec: https://msdn.microsoft.com/en-us/library/dd302994.aspx
        @param w: TdsWriter
        @return:
        """
        w.write_b_varchar("")  # db_name, should be empty
        w.write_b_varchar(self._table_type.typ_schema)
        w.write_b_varchar(self._table_type.typ_name)

    def write(self, w, val):
        """
        Writes remaining part of TVP_TYPE_INFO structure, resuming from TVP_COLMETADATA

        specs:
        https://msdn.microsoft.com/en-us/library/dd302994.aspx
        https://msdn.microsoft.com/en-us/library/dd305261.aspx
        https://msdn.microsoft.com/en-us/library/dd303230.aspx

        @param w: TdsWriter
        @param val: TableValuedParam or None
        @return:
        """
        if val.is_null():
            w.put_usmallint(TVP_NULL_TOKEN)
        else:
            columns = self._table_type.columns
            w.put_usmallint(len(columns))
            for i, column in enumerate(columns):
                w.put_uint(column.column_usertype)

                w.put_usmallint(column.flags)

                # TYPE_INFO structure: https://msdn.microsoft.com/en-us/library/dd358284.aspx

                serializer = self._columns_serializers[i]
                type_id = serializer.type
                w.put_byte(type_id)
                serializer.write_info(w)

                w.write_b_varchar('')  # ColName, must be empty in TVP according to spec

        # here can optionally send TVP_ORDER_UNIQUE and TVP_COLUMN_ORDERING
        # https://msdn.microsoft.com/en-us/library/dd305261.aspx

        # terminating optional metadata
        w.put_byte(TVP_END_TOKEN)

        # now sending rows using TVP_ROW
        # https://msdn.microsoft.com/en-us/library/dd305261.aspx
        if val.rows:
            for row in val.rows:
                w.put_byte(TVP_ROW_TOKEN)
                for i, col in enumerate(self._table_type.columns):
                    if not col.flags & TVP_COLUMN_DEFAULT_FLAG:
                        self._columns_serializers[i].write(w, row[i])

        # terminating rows
        w.put_byte(TVP_END_TOKEN)


_type_map = {
    SYBINT1: TinyIntSerializer,
    SYBINT2: SmallIntSerializer,
    SYBINT4: IntSerializer,
    SYBINT8: BigIntSerializer,
    SYBINTN: IntNSerializer,
    SYBBIT: BitSerializer,
    SYBBITN: BitNSerializer,
    SYBREAL: RealSerializer,
    SYBFLT8: FloatSerializer,
    SYBFLTN: FloatNSerializer,
    SYBMONEY4: Money4Serializer,
    SYBMONEY: Money8Serializer,
    SYBMONEYN: MoneyNSerializer,
    XSYBCHAR: VarChar70Serializer,
    XSYBVARCHAR: VarChar70Serializer,
    XSYBNCHAR: NVarChar70Serializer,
    XSYBNVARCHAR: NVarChar70Serializer,
    SYBTEXT: Text70Serializer,
    SYBNTEXT: NText70Serializer,
    SYBMSXML: XmlSerializer,
    XSYBBINARY: VarBinarySerializer,
    XSYBVARBINARY: VarBinarySerializer,
    SYBIMAGE: Image70Serializer,
    SYBNUMERIC: MsDecimalSerializer,
    SYBDECIMAL: MsDecimalSerializer,
    SYBVARIANT: VariantSerializer,
    SYBMSDATE: MsDateSerializer,
    SYBMSTIME: MsTimeSerializer,
    SYBMSDATETIME2: DateTime2Serializer,
    SYBMSDATETIMEOFFSET: DateTimeOffsetSerializer,
    SYBDATETIME4: SmallDateTimeSerializer,
    SYBDATETIME: DateTimeSerializer,
    SYBDATETIMN: DateTimeNSerializer,
    SYBUNIQUE: MsUniqueSerializer,
}

_type_map71 = _type_map.copy()
_type_map71.update({
    XSYBCHAR: VarChar71Serializer,
    XSYBNCHAR: NVarChar71Serializer,
    XSYBVARCHAR: VarChar71Serializer,
    XSYBNVARCHAR: NVarChar71Serializer,
    SYBTEXT: Text71Serializer,
    SYBNTEXT: NText71Serializer,
})

_type_map72 = _type_map.copy()
_type_map72.update({
    XSYBCHAR: VarChar72Serializer,
    XSYBNCHAR: NVarChar72Serializer,
    XSYBVARCHAR: VarChar72Serializer,
    XSYBNVARCHAR: NVarChar72Serializer,
    SYBTEXT: Text72Serializer,
    SYBNTEXT: NText72Serializer,
    XSYBBINARY: VarBinarySerializer72,
    XSYBVARBINARY: VarBinarySerializer72,
    SYBIMAGE: Image72Serializer,
})

_type_map73 = _type_map72.copy()
_type_map73.update({
    TVPTYPE: TableSerializer,
})


class SerializerFactory(object):
    """
    Factory class for TDS data types
    """
    def __init__(self, tds_ver):
        self._tds_ver = tds_ver
        if self._tds_ver >= TDS73:
            self._type_map = _type_map73
        elif self._tds_ver >= TDS72:
            self._type_map = _type_map72
        elif self._tds_ver >= TDS71:
            self._type_map = _type_map71
        else:
            self._type_map = _type_map

    def get_type_serializer(self, tds_type_id):
        type_class = self._type_map.get(tds_type_id)
        if not type_class:
            raise InterfaceError('Invalid type id {}'.format(tds_type_id))
        return type_class

    def long_binary_type(self):
        if self._tds_ver >= TDS72:
            return VarBinaryMaxType()
        else:
            return ImageType()

    def long_varchar_type(self):
        if self._tds_ver >= TDS72:
            return VarCharMaxType()
        else:
            return TextType()

    def long_string_type(self):
        if self._tds_ver >= TDS72:
            return NVarCharMaxType()
        else:
            return NTextType()

    def datetime(self, precision):
        if self._tds_ver >= TDS72:
            return DateTime2Type(precision=precision)
        else:
            return DateTimeType()

    def has_datetime_with_tz(self):
        return self._tds_ver >= TDS72

    def datetime_with_tz(self, precision):
        if self._tds_ver >= TDS72:
            return DateTimeOffsetType(precision=precision)
        else:
            raise DataError('Given TDS version does not support DATETIMEOFFSET type')

    def date(self):
        if self._tds_ver >= TDS72:
            return DateType()
        else:
            return DateTimeType()

    def time(self, precision):
        if self._tds_ver >= TDS72:
            return TimeType(precision=precision)
        else:
            raise DataError('Given TDS version does not support TIME type')

    def sql_type_by_declaration(self, declaration):
        return _declarations_parser.parse(declaration)

    def serializer_by_declaration(self, declaration, connection):
        sql_type = self.sql_type_by_declaration(declaration)
        return self.serializer_by_type(sql_type=sql_type, collation=connection.collation)

    def serializer_by_type(self, sql_type, collation=raw_collation):
        typ = sql_type
        if isinstance(typ, BitType):
            return BitNSerializer(typ)
        elif isinstance(typ, TinyIntType):
            return IntNSerializer(typ)
        elif isinstance(typ, SmallIntType):
            return IntNSerializer(typ)
        elif isinstance(typ, IntType):
            return IntNSerializer(typ)
        elif isinstance(typ, BigIntType):
            return IntNSerializer(typ)
        elif isinstance(typ, RealType):
            return FloatNSerializer(size=4)
        elif isinstance(typ, FloatType):
            return FloatNSerializer(size=8)
        elif isinstance(typ, SmallMoneyType):
            return self._type_map[SYBMONEYN](size=4)
        elif isinstance(typ, MoneyType):
            return self._type_map[SYBMONEYN](size=8)
        elif isinstance(typ, CharType):
            return self._type_map[XSYBCHAR](size=typ.size, collation=collation)
        elif isinstance(typ, VarCharType):
            return self._type_map[XSYBVARCHAR](size=typ.size, collation=collation)
        elif isinstance(typ, VarCharMaxType):
            return VarCharMaxSerializer(collation=collation)
        elif isinstance(typ, NCharType):
            return self._type_map[XSYBNCHAR](size=typ.size, collation=collation)
        elif isinstance(typ, NVarCharType):
            return self._type_map[XSYBNVARCHAR](size=typ.size, collation=collation)
        elif isinstance(typ, NVarCharMaxType):
            return NVarCharMaxSerializer(collation=collation)
        elif isinstance(typ, TextType):
            return self._type_map[SYBTEXT](collation=collation)
        elif isinstance(typ, NTextType):
            return self._type_map[SYBNTEXT](collation=collation)
        elif isinstance(typ, XmlType):
            return self._type_map[SYBMSXML]()
        elif isinstance(typ, BinaryType):
            return self._type_map[XSYBBINARY]()
        elif isinstance(typ, VarBinaryType):
            return self._type_map[XSYBVARBINARY](size=typ.size)
        elif isinstance(typ, VarBinaryMaxType):
            return VarBinarySerializerMax()
        elif isinstance(typ, ImageType):
            return self._type_map[SYBIMAGE]()
        elif isinstance(typ, DecimalType):
            return self._type_map[SYBDECIMAL](scale=typ.scale, prec=typ.precision)
        elif isinstance(typ, VariantType):
            return self._type_map[SYBVARIANT](size=0)
        elif isinstance(typ, SmallDateTimeType):
            return self._type_map[SYBDATETIMN](size=4)
        elif isinstance(typ, DateTimeType):
            return self._type_map[SYBDATETIMN](size=8)
        elif isinstance(typ, DateType):
            return self._type_map[SYBMSDATE](typ)
        elif isinstance(typ, TimeType):
            return self._type_map[SYBMSTIME](typ)
        elif isinstance(typ, DateTime2Type):
            return self._type_map[SYBMSDATETIME2](typ)
        elif isinstance(typ, DateTimeOffsetType):
            return self._type_map[SYBMSDATETIMEOFFSET](typ)
        elif isinstance(typ, UniqueIdentifierType):
            return self._type_map[SYBUNIQUE]()
        elif isinstance(typ, TableType):
            columns_serializers = [self.serializer_by_type(col.type) for col in typ.columns]
            return TableSerializer(table_type=typ, columns_serializers=columns_serializers)
        else:
            raise ValueError('Cannot map type {} to serializer.'.format(typ))


class DeclarationsParser(object):
    def __init__(self):
        declaration_parsers = [
            ('bit', BitType),
            ('tinyint', TinyIntType),
            ('smallint', SmallIntType),
            ('(?:int|integer)', IntType),
            ('bigint', BigIntType),
            ('real', RealType),
            ('(?:float|double precision)', FloatType),
            ('(?:char|character)', CharType),
            (r'(?:char|character)\((\d+)\)', lambda size_str: CharType(size=int(size_str))),
            (r'(?:varchar|char(?:|acter)\s+varying)', VarCharType),
            (r'(?:varchar|char(?:|acter)\s+varying)\((\d+)\)', lambda size_str: VarCharType(size=int(size_str))),
            (r'varchar\(max\)', VarCharMaxType),
            (r'(?:nchar|national\s+(?:char|character))', NCharType),
            (r'(?:nchar|national\s+(?:char|character))\((\d+)\)', lambda size_str: NCharType(size=int(size_str))),
            (r'(?:nvarchar|national\s+(?:char|character)\s+varying)', NVarCharType),
            (r'(?:nvarchar|national\s+(?:char|character)\s+varying)\((\d+)\)',
             lambda size_str: NVarCharType(size=int(size_str))),
            (r'nvarchar\(max\)', NVarCharMaxType),
            ('xml', XmlType),
            ('text', TextType),
            (r'(?:ntext|national\s+text)', NTextType),
            ('binary', BinaryType),
            ('binary\((\d+)\)', lambda size_str: BinaryType(size=int(size_str))),
            ('(?:varbinary|binary varying)', VarBinaryType),
            (r'(?:varbinary|binary varying)\((\d+)\)', lambda size_str: VarBinaryType(size=int(size_str))),
            (r'varbinary\(max\)', VarBinaryMaxType),
            ('image', ImageType),
            ('smalldatetime', SmallDateTimeType),
            ('datetime', DateTimeType),
            ('date', DateType),
            (r'time', TimeType),
            (r'time\((\d+)\)', lambda precision_str: TimeType(precision=int(precision_str))),
            ('datetime2', DateTime2Type),
            (r'datetime2\((\d+)\)', lambda precision_str: DateTime2Type(precision=int(precision_str))),
            ('datetimeoffset', DateTimeOffsetType),
            (r'datetimeoffset\((\d+)\)',
             lambda precision_str: DateTimeOffsetType(precision=int(precision_str))),
            ('(?:decimal|dec|numeric)', DecimalType),
            ('(?:decimal|dec|numeric)\((\d+)\)',
             lambda precision_str: DecimalType(precision=int(precision_str))),
            ('(?:decimal|dec|numeric)\((\d+), (\d+)\)',
             lambda precision_str, scale_str: DecimalType(precision=int(precision_str), scale=int(scale_str))),
            ('smallmoney', SmallMoneyType),
            ('money', MoneyType),
            ('uniqueidentifier', UniqueIdentifierType),
            ('sql_variant', VariantType),
        ]
        self._compiled = [(re.compile(r'^' + regex + '$', re.IGNORECASE), constructor)
                          for regex, constructor in declaration_parsers]

    def parse(self, declaration):
        """
        Parse sql type declaration, e.g. varchar(10) and return instance of corresponding type class, e.g. VarCharType(10)
        @param declaration: Sql declaration to parse, e.g. varchar(10)
        @return: instance of SqlTypeMetaclass
        """
        declaration = declaration.strip()
        for regex, constructor in self._compiled:
            m = regex.match(declaration)
            if m:
                return constructor(*m.groups())
        raise ValueError('Unable to parse type declaration', declaration)


_declarations_parser = DeclarationsParser()


class TdsTypeInferrer(object):
    def __init__(self, type_factory, collation=None, bytes_to_unicode=False, allow_tz=False):
        """
        Class used to do TDS type inference

        :param type_factory: Instance of TypeFactory
        :param collation: Collation to use for strings
        :param bytes_to_unicode: Treat bytes type as unicode string
        :param allow_tz: Allow usage of DATETIMEOFFSET type
        """
        self._type_factory = type_factory
        self._collation = collation
        self._bytes_to_unicode = bytes_to_unicode
        self._allow_tz = allow_tz

    def from_value(self, value):
        """ Function infers TDS type from Python value.

        :param value: value from which to infer TDS type
        :return: An instance of subclass of :class:`BaseType`
        """
        if value is None:
            sql_type = NVarCharType(size=1)
        else:
            sql_type = self._from_class_value(value, type(value))
        return sql_type

    def from_class(self, cls):
        """ Function infers TDS type from Python class.

        :param cls: Class from which to infer type
        :return: An instance of subclass of :class:`BaseType`
        """
        return self._from_class_value(None, cls)

    def _from_class_value(self, value, value_type):
        type_factory = self._type_factory
        bytes_to_unicode = self._bytes_to_unicode
        allow_tz = self._allow_tz

        if issubclass(value_type, bool):
            return BitType()
        elif issubclass(value_type, six.integer_types):
            if value is None:
                return IntType()
            if -2 ** 31 <= value <= 2 ** 31 - 1:
                return IntType()
            elif -2 ** 63 <= value <= 2 ** 63 - 1:
                return BigIntType()
            elif -10 ** 38 + 1 <= value <= 10 ** 38 - 1:
                return DecimalType(precision=38)
            else:
                return VarCharMaxType()
        elif issubclass(value_type, float):
            return FloatType()
        elif issubclass(value_type, Binary):
            if value is None or len(value) <= 8000:
                return VarBinaryType(size=8000)
            else:
                return type_factory.long_binary_type()
        elif issubclass(value_type, six.binary_type):
            if bytes_to_unicode:
                return type_factory.long_string_type()
            else:
                return type_factory.long_varchar_type()
        elif issubclass(value_type, six.string_types):
            return type_factory.long_string_type()
        elif issubclass(value_type, datetime):
            if value and value.tzinfo and allow_tz:
                return type_factory.datetime_with_tz(precision=6)
            else:
                return type_factory.datetime(precision=6)
        elif issubclass(value_type, date):
            return type_factory.date()
        elif issubclass(value_type, time):
            return type_factory.time(precision=6)
        elif issubclass(value_type, Decimal):
            if value is None:
                return DecimalType()
            else:
                return DecimalType.from_value(value)
        elif issubclass(value_type, uuid.UUID):
            return UniqueIdentifierType()
        elif issubclass(value_type, TableValuedParam):
            columns = value.columns
            rows = value.rows
            if columns is None:
                # trying to auto detect columns using data from first row
                if rows is None:
                    # rows are not present too, this means
                    # entire tvp has value of NULL
                    pass
                else:
                    # use first row to infer types of columns
                    row = value.peek_row()
                    columns = []
                    try:
                        cell_iter = iter(row)
                    except TypeError:
                        raise DataError('Each row in table should be an iterable')
                    for cell in cell_iter:
                        if isinstance(cell, TableValuedParam):
                            raise DataError('TVP type cannot have nested TVP types')
                        col_type = self.from_value(cell)
                        col = Column(type=col_type)
                        columns.append(col)

            return TableType(typ_schema=value.typ_schema, typ_name=value.typ_name, columns=columns)
        else:
            raise DataError('Cannot infer TDS type from Python value: {!r}'.format(value))