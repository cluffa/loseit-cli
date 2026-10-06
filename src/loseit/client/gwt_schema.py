"""Extract GWT-RPC field layouts from LoseIt's compiled JavaScript, and use them
to decode responses and encode requests.

GWT compiles field serializers per type into the permutation script
(`<strong name>.cache.js`) and registers them in a table:

    a['com.foo.Bar/123'] = [instantiate, deserialize, serialize]

(types the client only sends have `[undefined, undefined, serialize]`). Each
function is a fixed sequence of stream operations. Names are obfuscated, but
the stream primitives are recognizable by body shape:

    read                                  write
    a.b[--a.a]          int-like          kHd(a.a,''+x)     number
    YGd(a,a.b[--a.a])   string            fHd(a,''+SGd(a,x)) string (table ref)
    RGd(a)              object            WGd(a,x)          object
    _Gd(a)              double            fHd(a,QGd(x))     long (base64)
    bHd(a)              long              ...x?'1':'0'      boolean
    $Gd(a)              boolean

Every function is compiled into a list of ops, inlining helper/super calls
and turning `for` loops into repeat ops. Field *names* are lost; the model
layer maps positions to meaning. Serializers write fields in the same order
the deserializers read them, so a decoded object can be written back as-is.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterator


# Ops
INT, DOUBLE, LONG, STRING, BOOL, OBJECT = "i", "d", "l", "s", "b", "o"
NUMBER = "n"        # write side: int or double, formatted like JS ''+x
REPEAT = "rep"      # ["rep", ops]: preceded in the stream by an item count
ARRAY = "arr"       # ["arr", ops]: count = array length read by instantiate

PRIMITIVE_PARAMS = {"I": INT, "Z": BOOL, "D": DOUBLE, "F": DOUBLE, "J": LONG,
                    "B": INT, "S": INT, "C": INT}
STRING_SIG = "java.lang.String/2004016611"


class SchemaError(Exception):
    pass


_LONG_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789$_"


def encode_long(value: int) -> str:
    """Encode a long in GWT's base64 form (e.g. Date millis); 64-bit two's complement."""
    value &= (1 << 64) - 1
    if value == 0:
        return "A"
    out = []
    while value:
        out.append(_LONG_ALPHABET[value & 63])
        value >>= 6
    return "".join(reversed(out))


def decode_long(text: str) -> int:
    value = 0
    for ch in text:
        value = value * 64 + _LONG_ALPHABET.index(ch)
    value &= (1 << 64) - 1
    return value - (1 << 64) if value >= 1 << 63 else value


# ──────────────────────────────────────────────
# Extraction
# ──────────────────────────────────────────────

def _function_bodies(js: str) -> dict[str, tuple[list[str], str]]:
    funcs = {}
    for m in re.finditer(r"function ([\w$]+)\(([\w$,]*)\)\{", js):
        end, body = _matching_brace(js, m.end() - 1)
        funcs[m.group(1)] = (m.group(2).split(",") if m.group(2) else [], body)
    return funcs


def _matching_brace(s: str, open_idx: int) -> tuple[int, str]:
    depth, j = 1, open_idx + 1
    while depth:
        depth += (s[j] == "{") - (s[j] == "}")
        j += 1
    return j, s[open_idx + 1:j - 1]


def _matching_paren(s: str, open_idx: int) -> int:
    depth, j = 1, open_idx + 1
    while depth:
        depth += (s[j] == "(") - (s[j] == ")")
        j += 1
    return j


class _Compiler:
    def __init__(self, js: str):
        self.funcs = _function_bodies(js)
        self.read_prims = self._find_read_primitives()
        self.write_prims = self._find_write_primitives()

    def _body_is(self, name: str, pattern: str) -> bool:
        body = self.funcs[name][1]
        return re.fullmatch(rf"var [\w$,]+;ATv\[[\w$]+=\+\+BTv\]={re.escape(name)};{pattern}", body) is not None

    def _find_read_primitives(self) -> dict[str, str]:
        prims = {}
        for name, (params, body) in self.funcs.items():
            if len(params) == 1:
                r = re.escape(params[0])
                if re.search(rf"=Number\({r}\.b\[--{r}\.a\]\)", body):
                    prims[name] = DOUBLE
                elif re.search(rf"=!!{r}\.b\[--{r}\.a\]", body):
                    prims[name] = BOOL
                elif re.search(rf"b={r}\.b\[--{r}\.a\];if\(b<0\)", body):
                    prims[name] = OBJECT
                elif re.match(rf"var a={r}\.b\[--{r}\.a\],c,d;", body):
                    prims[name] = LONG
            elif len(params) == 2 and re.fullmatch(
                    rf"var c;ATv\[c=\+\+BTv\]={re.escape(name)};BTv=c-1;return a>0\?b\.d\[a-1\]:null", body):
                prims[name] = STRING
        if STRING not in prims.values() or OBJECT not in prims.values():
            raise SchemaError("Could not identify GWT stream reader primitives")
        return prims

    def _find_write_primitives(self) -> dict[str, str]:
        """Map writer functions to op kinds. 'raw' appends its argument verbatim."""
        prims = {}
        for name, (params, body) in self.funcs.items():
            if len(params) != 2:
                if len(params) == 1 and ">>28&15" in body:
                    prims[name] = "longenc"
                continue
            if "could not get type signature" in body:
                prims[name] = OBJECT
            elif self._body_is(name, r"[\w$]+\(a\.a,b\);BTv=c-1") or \
                    self._body_is(name, r"[\w$]+\(\);a\.a\+=''\+b;a\.a\+='\|';BTv=c-1"):
                prims[name] = "raw"
            elif self._body_is(name, r"[\w$]+\(a\.a,b\?'1':'0'\);BTv=c-1"):
                prims[name] = BOOL
            elif self._body_is(name, r"[\w$]+\(a\.a,''\+b\);BTv=c-1"):
                prims[name] = NUMBER
            elif "lm(a.i,b)" in body or re.search(r"lm\(a\.i,b\);c=a\.i\.a\.length", body):
                prims[name] = "strref"
        # writeString: fHd(a,''+<strref>(a,b))
        refs = [k for k, v in prims.items() if v == "strref"]
        for name, (params, body) in self.funcs.items():
            if len(params) == 2 and name not in prims:
                for ref in refs:
                    if self._body_is(name, rf"[\w$]+\(a,''\+{re.escape(ref)}\(a,b\)\);BTv=c-1"):
                        prims[name] = STRING
        if OBJECT not in prims.values() or "raw" not in prims.values():
            raise SchemaError("Could not identify GWT stream writer primitives")
        return prims

    # reading
    def compile_read(self, fname: str, depth: int = 0) -> list:
        if depth > 20:
            raise SchemaError(f"Recursion too deep in {fname}")
        params, body = self.funcs[fname]
        return self._compile_read(body, params[0], depth) if params else []

    def _compile_read(self, body: str, r: str, depth: int) -> list:
        rx = re.escape(r)
        string_fn = next(k for k, v in self.read_prims.items() if v == STRING)
        token = re.compile(
            rf"(?P<for>(?<![\w$])for\()"
            rf"|(?P<str>{re.escape(string_fn)}\({rx},{rx}\.b\[--{rx}\.a\]\))"
            rf"|(?P<call>(?<![\w$.])(?P<fn>[\w$]+)\({rx}(?=[,)]))"
            rf"|(?P<int>{rx}\.b\[--{rx}\.a\])"
        )
        ops: list = []
        pos = 0
        while m := token.search(body, pos):
            pos = m.end()
            if m.group("for"):
                pos = self._loop(body, m, ops, lambda inner: self._compile_read(inner, r, depth + 1), INT)
            elif m.group("str"):
                ops.append(STRING)
            elif m.group("call"):
                fn = m.group("fn")
                if fn in self.read_prims:
                    ops.append(self.read_prims[fn])
                elif fn in self.funcs:
                    ops.extend(self.compile_read(fn, depth + 1))
            else:
                ops.append(INT)
        return ops

    # writing
    def compile_write(self, fname: str, depth: int = 0) -> list:
        if depth > 20:
            raise SchemaError(f"Recursion too deep in {fname}")
        params, body = self.funcs[fname]
        return self._compile_write(body, params[0], depth) if params else []

    def _compile_write(self, body: str, w: str, depth: int) -> list:
        wx = re.escape(w)
        token = re.compile(rf"(?P<for>(?<![\w$])for\()|(?<![\w$.])(?P<fn>[\w$]+)\((?P<arg0>{wx}|{wx}\.a)(?=,)")
        ops: list = []
        pos = 0
        while m := token.search(body, pos):
            if m.group("for"):
                pos = self._loop(body, m, ops, lambda inner: self._compile_write(inner, w, depth + 1), NUMBER)
                continue
            fn = m.group("fn")
            close = _matching_paren(body, m.end("arg0") - len(m.group("arg0")) - 1)
            arg = body[m.end("arg0") + 1:close - 1]
            pos = close
            kind = self.write_prims.get(fn)
            if kind == "raw":
                ops.append(self._classify_raw(arg))
            elif kind in (OBJECT, STRING, BOOL, NUMBER):
                ops.append(kind)
            elif kind is None and fn in self.funcs and m.group("arg0") == w:
                ops.extend(self.compile_write(fn, depth + 1))
        return ops

    def _classify_raw(self, arg: str) -> str:
        if "?'1':'0'" in arg:
            return BOOL
        call = re.match(r"(?:''\+)?([\w$]+)\(", arg)
        if call and self.write_prims.get(call.group(1)) == "longenc":
            return LONG
        if call and self.write_prims.get(call.group(1)) == "strref":
            return STRING
        return NUMBER

    def _loop(self, body: str, m, ops: list, compile_inner, count_op: str) -> int:
        head_end = body.index("{", m.end())
        head = body[m.end():head_end]
        end, inner = _matching_brace(body, head_end)
        loop_ops = compile_inner(inner)
        if ".length" in head and count_op == INT:
            ops.append([ARRAY, loop_ops])
        else:
            if not ops or ops[-1] != count_op:
                raise SchemaError(f"Loop without count: {head}")
            ops[-1] = [REPEAT, loop_ops]
        return end


def extract_schema(js: str) -> dict[str, dict]:
    """Return {type_signature: {"inst", "de", "ser": ops | None, "enum": bool}}."""
    consts = dict(re.findall(r"([\w$]+)='((?:[^'\\]|\\.)*)'", js))
    comp = _Compiler(js)
    schema: dict[str, dict] = {}
    for m in re.finditer(r"a\[([\w$]+)\]=\[([\w$]+),([\w$]+)(?:,([\w$]+))?\]", js):
        sig = consts.get(m.group(1))
        if not sig or "/" not in sig:
            continue
        inst, de, ser = (g if g in comp.funcs else None for g in m.group(2, 3, 4))
        entry = schema.setdefault(sig, {"inst": None, "de": None, "ser": None, "enum": False})
        if inst and de:
            entry["inst"] = comp.compile_read(inst)
            entry["de"] = comp.compile_read(de)
            entry["enum"] = bool(re.search(r"return [\w$]+\[[\w$]+\]$", comp.funcs[inst][1]))
        if ser:
            entry["ser"] = comp.compile_write(ser)
    if not schema:
        raise SchemaError("No type serializers found in script")
    return schema


# ──────────────────────────────────────────────
# Values
# ──────────────────────────────────────────────

@dataclass
class GObject:
    """A Java object: short type name, positional fields, and signature.

    `init` holds values read by the instantiator (e.g. a Timestamp's millis),
    which serializers write before the fields.
    """
    type: str
    fields: list = field(default_factory=list)
    signature: str = ""
    init: list = field(default_factory=list, repr=False)

    def __repr__(self) -> str:
        return f"{self.type}{self.fields!r}"


@dataclass(frozen=True)
class GEnum:
    type: str
    ordinal: int
    signature: str = field(default="", compare=False, repr=False)


class _Boxed:
    """Mixin marking a boxed Java value (Double, Integer, String, Date...)."""
    signature: str = ""


class BoxedFloat(_Boxed, float): pass
class BoxedInt(_Boxed, int): pass
class BoxedStr(_Boxed, str): pass


def box(value, signature: str):
    """Wrap a Python value as a boxed Java object of the given type."""
    cls = BoxedStr if isinstance(value, str) else BoxedFloat if isinstance(value, float) else BoxedInt
    v = cls(value)
    v.signature = signature
    return v


def short_name(sig: str) -> str:
    return sig.split("/")[0].split(".")[-1]


# Types whose single list field is the whole content
_COLLECTIONS = {"ArrayList", "LinkedList", "HashSet", "LinkedHashSet", "TreeSet",
                "HashMap", "LinkedHashMap", "TreeMap", "Arrays$ArrayList"}


# ──────────────────────────────────────────────
# Decoding
# ──────────────────────────────────────────────

class _Decoder:
    """Shared object-graph reader; subclasses supply the token source."""

    def __init__(self, schema: dict[str, dict]):
        self.schema = schema

    def _raw(self): raise NotImplementedError
    def _str_at(self, idx: int): raise NotImplementedError

    def _read(self, op):
        if op == OBJECT:
            return self._read_object()
        v = self._raw()
        if op == STRING:
            return self._str_at(int(v))
        if op == LONG:
            return decode_long(v)
        if op == DOUBLE:
            return float(v)
        if op == BOOL:
            return bool(int(v))
        return int(v)

    def _read_object(self):
        token = int(self._raw())
        if token < 0:
            return self._objects[-(token + 1)]
        if token == 0:
            return None
        sig = self._str_at(token)
        spec = self.schema.get(sig)
        if spec is None or spec["de"] is None:
            raise SchemaError(f"No deserializer for type: {sig}")
        slot = len(self._objects)
        self._objects.append(None)

        inst_vals = self._run(spec["inst"])
        name = short_name(sig)
        if not spec["de"] and len(inst_vals) == 1 and not isinstance(inst_vals[0], list):
            v = inst_vals[0]
            value = GEnum(name, v, sig) if spec["enum"] else (None if v is None else box(v, sig))
            self._objects[slot] = value
            return value

        obj = GObject(name, [], sig)
        self._objects[slot] = obj
        is_array = any(isinstance(op, list) and op[0] == ARRAY for op in spec["de"])
        length = inst_vals[-1] if is_array else 0
        obj.init = [] if is_array else inst_vals
        obj.fields = self._run(spec["de"], array_len=length)
        if name in _COLLECTIONS and len(obj.fields) == 1 and isinstance(obj.fields[0], list):
            obj.fields = obj.fields[0]
        return obj

    def _run(self, ops: list, array_len: int = 0) -> list:
        out = []
        for op in ops:
            if isinstance(op, str):
                out.append(self._read(op))
                continue
            kind, inner = op
            count = int(self._raw()) if kind == REPEAT else array_len
            items = []
            for _ in range(count):
                vals = self._run(inner)
                items.append(vals[0] if len(vals) == 1 else tuple(vals))
            out.append(items)
        return out


class ResponseDecoder(_Decoder):
    """Decode a `//OK[...]` response (values are read from the end)."""

    def decode(self, raw: str) -> Any:
        if not raw.startswith("//OK"):
            raise SchemaError(f"Not a success response: {raw[:80]}")
        arr = _parse_payload(raw[4:])
        self._strings = arr[-3]
        self._vals = arr[:-3]
        self._pos = len(self._vals)
        self._objects: list = []
        result = self._read_object()
        if self._pos != 0:
            raise SchemaError(f"{self._pos} values left unread (schema mismatch?)")
        return result

    def _raw(self):
        if self._pos <= 0:
            raise SchemaError("Read past start of stream")
        self._pos -= 1
        return self._vals[self._pos]

    def _str_at(self, idx: int):
        return self._strings[idx - 1] if idx > 0 else None


def _parse_payload(body: str) -> list:
    """Parse a response payload. Large ones are split into chunks:
    `[a].concat([b],[c])` (possibly chained), which is joined here."""
    dec = json.JSONDecoder()
    arr, pos = dec.raw_decode(body)
    while body.startswith(".concat(", pos):
        pos += len(".concat(")
        while True:
            chunk, pos = dec.raw_decode(body, pos)
            arr.extend(chunk)
            if body[pos] == ",":
                pos += 1
                continue
            if body[pos] != ")":
                raise SchemaError("Malformed chunked response")
            pos += 1
            break
    if body[pos:].strip():
        raise SchemaError("Trailing data after response payload")
    return arr


@dataclass
class RpcCall:
    method: str
    param_types: list[str]
    params: list
    base_url: str = ""
    policy_hash: str = ""
    service: str = ""


class RequestDecoder(_Decoder):
    """Decode a client request body (values are read front to back)."""

    def decode(self, raw: str) -> RpcCall:
        parts = raw.rstrip("|").split("|")
        n = int(parts[2])
        self._strings = parts[3:3 + n]
        self._tokens = parts[3 + n:]
        self._pos = 0
        self._objects = []
        base, policy, service, method = (self._read(STRING) for _ in range(4))
        types = [self._read(STRING) for _ in range(int(self._raw()))]
        params = [self._read_param(t) for t in types]
        if self._pos != len(self._tokens):
            raise SchemaError(f"{len(self._tokens) - self._pos} values left unread")
        return RpcCall(method, types, params, base, policy, service)

    def _read_param(self, sig: str):
        if sig in PRIMITIVE_PARAMS:
            return self._read(PRIMITIVE_PARAMS[sig])
        if sig == STRING_SIG:
            return self._read(STRING)
        return self._read_object()

    def _read(self, op):
        if op == NUMBER:
            v = self._raw()
            return float(v) if any(c in v for c in ".eE") or v in ("NaN", "Infinity") else int(v)
        return super()._read(op)

    def _read_object(self):
        """Requests follow the `ser` layout (counts precede every loop)."""
        token = int(self._raw())
        if token < 0:
            return self._objects[-(token + 1)]
        if token == 0:
            return None
        sig = self._str_at(token)
        spec = self.schema.get(sig)
        if spec is None or spec["ser"] is None:
            raise SchemaError(f"No serializer for type: {sig}")
        slot = len(self._objects)
        self._objects.append(None)
        name = short_name(sig)
        ser = spec["ser"]
        if spec["enum"]:
            value = GEnum(name, self._read(ser[0]), sig)
            self._objects[slot] = value
            return value
        if len(ser) == 1 and isinstance(ser[0], str) and ser[0] != OBJECT and not spec["de"]:
            v = self._read(ser[0])
            value = None if v is None else box(v, sig)
            self._objects[slot] = value
            return value
        obj = GObject(name, [], sig)
        self._objects[slot] = obj
        values = self._run(ser)
        is_array = spec["de"] is not None and any(isinstance(op, list) and op[0] == ARRAY for op in spec["de"])
        n_init = len(spec["inst"]) if spec["inst"] and not is_array else 0
        obj.init, obj.fields = values[:n_init], values[n_init:]
        if name in _COLLECTIONS and len(obj.fields) == 1 and isinstance(obj.fields[0], list):
            obj.fields = obj.fields[0]
        return obj

    def _raw(self):
        if self._pos >= len(self._tokens):
            raise SchemaError("Read past end of request")
        self._pos += 1
        return self._tokens[self._pos - 1]

    def _str_at(self, idx: int):
        return self._strings[idx - 1] if idx > 0 else None


# ──────────────────────────────────────────────
# Encoding
# ──────────────────────────────────────────────

def _js_number(v) -> str:
    """Format a number the way JavaScript's ''+x does."""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 1e21:
            return str(int(v))
        return repr(float(v))
    return str(int(v))


class RequestEncoder:
    """Encode an RpcCall into a GWT-RPC v7 request body using `ser` layouts."""

    def __init__(self, schema: dict[str, dict]):
        self.schema = schema

    def encode(self, call: RpcCall) -> str:
        self._strings: list[str] = []
        self._out: list[str] = []
        self._seen: dict[int, int] = {}
        for s in (call.base_url, call.policy_hash, call.service, call.method):
            self._write_string(s)
        self._out.append(str(len(call.param_types)))
        for t in call.param_types:
            self._write_string(t)
        for sig, value in zip(call.param_types, call.params):
            if sig in PRIMITIVE_PARAMS:
                self._write(PRIMITIVE_PARAMS[sig], value)
            elif sig == STRING_SIG:
                self._write_string(value)
            else:
                self._write_object(value)
        head = ["7", "0", str(len(self._strings)), *self._strings]
        return "|".join(head + self._out) + "|"

    def _write_string(self, value):
        if value is None:
            self._out.append("0")
            return
        if value not in self._strings:
            self._strings.append(value)
        self._out.append(str(self._strings.index(value) + 1))

    def _write(self, op, value):
        if op == OBJECT:
            self._write_object(value)
        elif op == STRING:
            self._write_string(value)
        elif op == LONG:
            self._out.append(encode_long(int(value)))
        elif op == BOOL:
            self._out.append("1" if value else "0")
        else:
            self._out.append(_js_number(value))

    def _write_object(self, value):
        if value is None:
            self._out.append("0")
            return
        if id(value) in self._seen:
            self._out.append(str(-(self._seen[id(value)] + 1)))
            return
        sig = getattr(value, "signature", "")
        spec = self.schema.get(sig)
        if not sig or spec is None or spec["ser"] is None:
            raise SchemaError(f"No serializer for {type(value).__name__} {sig!r}")
        self._seen[id(value)] = len(self._seen)
        self._write_string(sig)

        if isinstance(value, GEnum):
            values = [value.ordinal]
        elif isinstance(value, GObject):
            fields = [value.fields] if value.type in _COLLECTIONS else value.fields
            values = list(value.init) + list(fields)
        else:  # boxed primitive
            values = [value.__class__.__mro__[2](value)]
        it = iter(values)
        self._run(spec["ser"], it)
        if next(it, _END) is not _END:
            raise SchemaError(f"Too many values for {sig}")

    def _run(self, ops: list, values: Iterator):
        for op in ops:
            if isinstance(op, str):
                self._write(op, next(values))
                continue
            items = next(values)
            self._out.append(str(len(items)))
            for item in items:
                self._run(op[1], iter(item if isinstance(item, tuple) else (item,)))


_END = object()


# ──────────────────────────────────────────────
# Service methods
# ──────────────────────────────────────────────

def extract_methods(js: str) -> dict[str, list[list[str]]]:
    """Return {method: [overload param type signatures, ...]} from the service proxy.

    Each proxy method looks like:
        g=new P(b,svc,'deleteFoodLogEntry');try{h=Q(g,iface,2);
          fHd(h,''+SGd(h,<type>));fHd(h,''+SGd(h,<type>));WGd(h,c);...
    """
    consts = dict(re.findall(r"([\w$]+)='((?:[^'\\]|\\.)*)'", js))
    methods = {}
    pattern = re.compile(
        r"new [\w$]+\([\w$]+,[\w$]+,('\w+'|[\w$]+)\);try\{([\w$]+)=[\w$]+\([\w$]+,[\w$]+,(\d+)\);"
        r"((?:[\w$]+\(\2,''\+[\w$]+\(\2,(?:[\w$]+|'[^']*')\)\);)*)")
    for m in pattern.finditer(js):
        refs = re.findall(r"\(%s,((?:[\w$]+|'[^']*'))\)\)" % re.escape(m.group(2)), m.group(4))
        types = [r.strip("'") if r.startswith("'") else consts.get(r, r) for r in refs]
        name = m.group(1).strip("'") if m.group(1).startswith("'") else consts.get(m.group(1))
        if name and len(types) == int(m.group(3)) and types not in methods.get(name, []):
            methods.setdefault(name, []).append(types)
    return methods


def build_schema(scripts: list[str]) -> dict:
    js = "\n".join(scripts)
    return {"types": extract_schema(js), "methods": extract_methods(js)}


# ──────────────────────────────────────────────
# Schema cache
# ──────────────────────────────────────────────

def schema_cache_path(permutation: str):
    from . import session as session_mod  # read at call time (tests patch it)
    return session_mod.SESSION_DIR / f"gwt-schema-{permutation}.json"


def load_schema(client, gwt_base_url: str, permutation: str) -> dict:
    """Load {"types", "methods"} for a permutation, building it from the JS on first use.

    Types and service methods are spread over the permutation script and its
    deferred fragments (deferredjs/<perm>/1..N.cache.js), so all are read.
    """
    path = schema_cache_path(permutation)
    if path.exists():
        try:
            schema = json.loads(path.read_text())
            if {"types", "methods"} <= schema.keys():
                return schema
        except json.JSONDecodeError:
            pass
    main = client.get(f"{gwt_base_url}{permutation}.cache.js")
    main.raise_for_status()
    scripts = [main.text]
    for n in range(1, 100):
        frag = client.get(f"{gwt_base_url}deferredjs/{permutation}/{n}.cache.js")
        if frag.status_code != 200:
            break
        scripts.append(frag.text)
    schema = build_schema(scripts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema))
    return schema
