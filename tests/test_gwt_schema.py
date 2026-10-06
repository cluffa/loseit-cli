"""Schema extraction, response/request decoding, and request encoding."""
import json
from pathlib import Path

import pytest

from loseit.client.gwt_schema import (
    GEnum, GObject, RequestDecoder, RequestEncoder, ResponseDecoder, SchemaError,
    decode_long, encode_long, extract_methods, extract_schema,
)

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA = json.loads((FIXTURES / "gwt_schema.json").read_text())
TYPES = SCHEMA["types"]

# A minimal script in the shape of a GWT permutation: reader and writer
# primitives, a model type with a super-class deserializer, an enum, an
# ArrayList with a counted loop, and a service proxy method.
SYNTHETIC_JS = """
function RGd(a){var b,c,d,e,f,g;ATv[g=++BTv]=RGd;b=a.b[--a.a];if(b<0){return 1}}
function YGd(b,a){var c;ATv[c=++BTv]=YGd;BTv=c-1;return a>0?b.d[a-1]:null}
function _Gd(a){var b,c;ATv[c=++BTv]=_Gd;b=Number(a.b[--a.a]);BTv=c-1;return b}
function bHd(b){var a=b.b[--b.a],c,d;ATv[d=++BTv]=bHd;c=PGd(a);BTv=d-1;return c}
function WGd(a,b){var c,d,e;ATv[e=++BTv]=WGd;throw new X('could not get type signature for ')}
function kHd(a,b){var c;ATv[c=++BTv]=kHd;eHd();a.a+=''+b;a.a+='|';BTv=c-1}
function fHd(a,b){var c;ATv[c=++BTv]=fHd;kHd(a.a,b);BTv=c-1}
function VGd(a,b){var c;ATv[c=++BTv]=VGd;kHd(a.a,''+b);BTv=c-1}
function SGd(a,b){var c,d,e;ATv[e=++BTv]=SGd;lm(a.i,b);c=a.i.a.length;return c}
function XGd(a,b){var c;ATv[c=++BTv]=XGd;fHd(a,''+SGd(a,b));BTv=c-1}
function QGd(a){var b;ATv[b=++BTv]=QGd;b=NGd(e,c>>28&15,false);return b}
function base(a,b){var c;ATv[c=++BTv]=base;b.x=a.b[--a.a];BTv=c-1}
function deChild(a,b){var c;ATv[c=++BTv]=deChild;set(b,YGd(a,a.b[--a.a]));b.y=_Gd(a);b.z=iP(RGd(a),5);base(a,b);BTv=c-1}
function serBase(a,b){var c;ATv[c=++BTv]=serBase;VGd(a,b.x);BTv=c-1}
function serChild(a,b){var c;ATv[c=++BTv]=serChild;XGd(a,b.s);fHd(a,''+b.y);WGd(a,b.z);serBase(a,b);BTv=c-1}
function inChild(a){var b,c;ATv[c=++BTv]=inChild;b=new X;BTv=c-1;return b}
function inEnum(a){var b,c,d;ATv[d=++BTv]=inEnum;b=a.b[--a.a];c=vals();BTv=d-1;return c[b]}
function deEnum(a,b){}
function serEnum(a,b){var c;ATv[c=++BTv]=serEnum;VGd(a,b.g);BTv=c-1}
function loopHelper(a,b){var c,d,e,f;ATv[f=++BTv]=loopHelper;e=a.b[--a.a];for(c=0;c<e;++c){d=RGd(a);b.hc(d)}BTv=f-1}
function deList(a,b){var c;ATv[c=++BTv]=deList;loopHelper(a,b);BTv=c-1}
function serList(a,b){var c,d,e,f;ATv[f=++BTv]=serList;e=b.lc();kHd(a.a,''+e);for(d=b.Yb();d.ec();){c=d.fc();WGd(a,c)}BTv=f-1}
function serDate(a,b){var c;ATv[c=++BTv]=serDate;fHd(a,QGd(b.$e()));BTv=c-1}
function inList(a){return new L}
var k1='com.x.Child/1',k2='com.x.Kind/2',k3='java.util.ArrayList/3',k4='java.util.Date/4',m1='doThing',t1='com.x.Token/9';
function init(){a[k1]=[inChild,deChild,serChild];a[k2]=[inEnum,deEnum,serEnum];a[k3]=[inList,deList,serList];a[k4]=[undefined,undefined,serDate]}
function proxy(b,c,d){var g,h;g=new rHd(b,w8v,m1);try{h=qHd(g,kow,2);fHd(h,''+SGd(h,t1));fHd(h,''+SGd(h,'I'));WGd(h,c);pHd(g,d)}catch(a){}}
"""


class TestExtract:
    def test_layouts(self):
        schema = extract_schema(SYNTHETIC_JS)
        child = schema["com.x.Child/1"]
        assert child["de"] == ["s", "d", "o", "i"]
        assert child["ser"] == ["s", "n", "o", "n"]
        assert schema["com.x.Kind/2"]["enum"] is True
        assert schema["java.util.ArrayList/3"]["de"] == [["rep", ["o"]]]
        assert schema["java.util.ArrayList/3"]["ser"] == [["rep", ["o"]]]
        # write-only type: no reader, serializer writes a long
        assert schema["java.util.Date/4"] == {"inst": None, "de": None, "ser": ["l"], "enum": False}

    def test_methods(self):
        assert extract_methods(SYNTHETIC_JS) == {"doThing": [["com.x.Token/9", "I"]]}

    def test_no_reader_primitives(self):
        with pytest.raises(SchemaError):
            extract_schema("function f(a){}")

    def test_synthetic_decode(self):
        schema = extract_schema(SYNTHETIC_JS)
        strings = ["java.util.ArrayList/3", "com.x.Child/1", "hello", "com.x.Kind/2"]
        # read order: list(2 items): Child("hello", 1.5, Kind#1, 7), backref to the Child
        read_order = [1, 2, 2, 3, 1.5, 4, 1, 7, -2]
        raw = "//OK" + json.dumps(list(reversed(read_order)) + [strings, 0, 7])
        result = ResponseDecoder(schema).decode(raw)
        child = result.fields[0]
        assert child.fields == ["hello", 1.5, GEnum("Kind", 1), 7]
        assert result.fields[1] is child

    def test_real_schema_has_logging_methods(self):
        for method in ("searchFoods", "getFood", "getUnsavedFoodLogEntry", "updateFoodLogEntry",
                       "deleteFoodLogEntry", "getDailyDetailsIncludingPendingForDate",
                       "getDailyDetailsIncludingPendingForDateRange"):
            assert method in SCHEMA["methods"]


class TestResponses:
    @pytest.mark.parametrize("path", sorted(FIXTURES.glob("*_response_*.txt")), ids=lambda p: p.name)
    def test_every_recorded_response_decodes_completely(self, path):
        raw = path.read_text().strip()
        if not raw.startswith("//OK"):
            pytest.skip("not a success response")
        assert isinstance(ResponseDecoder(TYPES).decode(raw), GObject)

    def test_leftover_values_are_an_error(self):
        raw = (FIXTURES / "getFood_response_01.txt").read_text().strip()
        arr = json.loads(raw[4:])
        arr.insert(0, 123)  # an extra value at the end of the read order
        with pytest.raises(SchemaError, match="left unread"):
            ResponseDecoder(TYPES).decode("//OK" + json.dumps(arr))

    def test_chunked_payload(self):
        raw = (FIXTURES / "getFood_response_01.txt").read_text().strip()
        arr = json.loads(raw[4:])
        chunked = f"//OK{json.dumps(arr[:5])}.concat({json.dumps(arr[5:10])},{json.dumps(arr[10:])})"
        assert ResponseDecoder(TYPES).decode(chunked) == ResponseDecoder(TYPES).decode(raw)


class TestRequests:
    @pytest.mark.parametrize("path", sorted(FIXTURES.glob("*_request_*.txt")), ids=lambda p: p.name)
    def test_recorded_request_roundtrips_byte_for_byte(self, path):
        raw = path.read_text().strip()
        call = RequestDecoder(TYPES).decode(raw)
        assert RequestEncoder(TYPES).encode(call) == raw

    def test_unknown_type_is_an_error(self):
        with pytest.raises(SchemaError, match="No serializer"):
            call = RequestDecoder(TYPES).decode((FIXTURES / "getFood_request_01.txt").read_text().strip())
            call.params[1] = GObject("Nope", [], "com.x.Nope/1")
            RequestEncoder(TYPES).encode(call)


@pytest.mark.parametrize("value", [0, 1, 63, 64, 1785128401000, 2**63 - 1, -1, -1785128401000])
def test_long_roundtrip(value):
    assert decode_long(encode_long(value)) == value


def test_long_known_value():
    assert decode_long("Z$h8bho") == 1785128401000
