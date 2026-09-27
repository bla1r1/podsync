"""The iPod model registry: model numbers, USB product ids and serial suffixes."""

from __future__ import annotations

import re

# (family, generation, "MODEL=capacity/color, ...") in table order.
_MODEL_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("iPod Classic", "6th Gen",
     "MB029=80GB/Silver, MB147=80GB/Black, MB145=160GB/Silver, MB150=160GB/Black"),
    ("iPod Classic", "6.5th Gen", "MB562=120GB/Silver, MB565=120GB/Black"),
    ("iPod Classic", "7th Gen", "MC293=160GB/Silver, MC297=160GB/Black"),
    ("iPod", "1st Gen",
     "M8513=5GB/White, M8541=5GB/White, M8697=5GB/White, M8709=10GB/White"),
    ("iPod", "2nd Gen",
     "M8737=10GB/White, M8740=10GB/White, M8738=20GB/White, M8741=20GB/White"),
    ("iPod", "3rd Gen",
     "M8976=10GB/White, M8946=15GB/White, M8948=30GB/White, M9244=20GB/White, "
     "M9245=40GB/White, M9460=15GB/White"),
    ("iPod", "4th Gen (mono)",
     "M9268=40GB/White, M9282=20GB/White, ME436=40GB/White, M9787=20GB/U2"),
    ("iPod", "4th Gen (photo)",
     "M9585=40GB/White, M9586=60GB/White, M9829=30GB/White, M9830=60GB/White, "
     "MS492=30GB/White"),
    ("iPod", "4th Gen (color)", "MA079=20GB/White, MA127=20GB/U2, MA215=20GB/White"),
    ("iPod", "5th Gen",
     "MA002=30GB/White, MA003=60GB/White, MA146=30GB/Black, MA147=60GB/Black, "
     "MA452=30GB/U2"),
    ("iPod", "5.5th Gen",
     "MA444=30GB/White, MA446=30GB/Black, MA448=80GB/White, MA450=80GB/Black, "
     "MA664=30GB/U2"),
    ("iPod Mini", "1st Gen",
     "M9160=4GB/Silver, M9434=4GB/Green, M9435=4GB/Pink, M9436=4GB/Blue, M9437=4GB/Gold"),
    ("iPod Mini", "2nd Gen",
     "M9800=4GB/Silver, M9801=6GB/Silver, M9802=4GB/Blue, M9803=6GB/Blue, M9804=4GB/Pink, "
     "M9805=6GB/Pink, M9806=4GB/Green, M9807=6GB/Green"),
    ("iPod Nano", "1st Gen",
     "MA004=2GB/White, MA005=4GB/White, MA099=2GB/Black, MA107=4GB/Black, "
     "MA350=1GB/White, MA352=1GB/Black"),
    ("iPod Nano", "2nd Gen",
     "MA426=4GB/Silver, MA428=4GB/Blue, MA477=2GB/Silver, MA487=4GB/Green, "
     "MA489=4GB/Pink, MA497=8GB/Black, MA725=4GB/Red, MA726=8GB/Red, MA899=8GB/Red"),
    ("iPod Nano", "3rd Gen",
     "MA978=4GB/Silver, MA980=8GB/Silver, MB249=8GB/Blue, MB253=8GB/Green, "
     "MB257=8GB/Red, MB261=8GB/Black, MB453=8GB/Pink"),
    ("iPod Nano", "4th Gen",
     "MB480=4GB/Silver, MB651=4GB/Blue, MB654=4GB/Pink, MB657=4GB/Purple, "
     "MB660=4GB/Orange, MB663=4GB/Green, MB666=4GB/Yellow, MB598=8GB/Silver, "
     "MB732=8GB/Blue, MB735=8GB/Pink, MB739=8GB/Purple, MB742=8GB/Orange, "
     "MB745=8GB/Green, MB748=8GB/Yellow, MB751=8GB/Red, MB754=8GB/Black, "
     "MB903=16GB/Silver, MB905=16GB/Blue, MB907=16GB/Pink, MB909=16GB/Purple, "
     "MB911=16GB/Orange, MB913=16GB/Green, MB915=16GB/Yellow, MB917=16GB/Red, "
     "MB918=16GB/Black"),
    ("iPod Nano", "5th Gen",
     "MC027=8GB/Silver, MC031=8GB/Black, MC034=8GB/Purple, MC037=8GB/Blue, "
     "MC040=8GB/Green, MC043=8GB/Yellow, MC046=8GB/Orange, MC049=8GB/Red, "
     "MC050=8GB/Pink, MC060=16GB/Silver, MC062=16GB/Black, MC064=16GB/Purple, "
     "MC066=16GB/Blue, MC068=16GB/Green, MC070=16GB/Yellow, MC072=16GB/Orange, "
     "MC074=16GB/Red, MC075=16GB/Pink"),
    ("iPod Nano", "6th Gen",
     "MC525=8GB/Silver, MC688=8GB/Graphite, MC689=8GB/Blue, MC690=8GB/Green, "
     "MC691=8GB/Orange, MC692=8GB/Pink, MC693=8GB/Red, MC526=16GB/Silver, "
     "MC694=16GB/Graphite, MC695=16GB/Blue, MC696=16GB/Green, MC697=16GB/Orange, "
     "MC698=16GB/Pink, MC699=16GB/Red"),
    ("iPod Nano", "7th Gen",
     "MD475=16GB/Pink, MD476=16GB/Yellow, MD477=16GB/Blue, MD478=16GB/Green, "
     "MD479=16GB/Purple, MD480=16GB/Silver, MD481=16GB/Slate, MD744=16GB/Red, "
     "ME971=16GB/Space Gray, MKMV2=16GB/Pink, MKMX2=16GB/Gold, MKN02=16GB/Blue, "
     "MKN22=16GB/Silver, MKN52=16GB/Space Gray, MKN72=16GB/Red"),
    ("iPod Shuffle", "1st Gen", "M9724=512MB/White, M9725=1GB/White"),
    ("iPod Shuffle", "2nd Gen",
     "MA546=1GB/Silver, MA564=1GB/Silver, MA947=1GB/Pink, MA949=1GB/Blue, "
     "MA951=1GB/Green, MA953=1GB/Orange, MB225=1GB/Silver, MB227=1GB/Blue, "
     "MB228=1GB/Blue, MB229=1GB/Green, MB231=1GB/Red, MB233=1GB/Purple, "
     "MB518=2GB/Silver, MB520=2GB/Blue, MB522=2GB/Green, MB524=2GB/Red, "
     "MB526=2GB/Purple, MB811=1GB/Pink, MB813=1GB/Blue, MB815=1GB/Green, "
     "MB817=1GB/Red, MB681=2GB/Pink, MB683=2GB/Blue, MB685=2GB/Green, "
     "MB779=2GB/Red, MC167=1GB/Gold"),
    ("iPod Shuffle", "3rd Gen",
     "MB867=4GB/Silver, MC164=4GB/Black, MC306=2GB/Silver, MC323=2GB/Black, "
     "MC381=2GB/Green, MC384=2GB/Blue, MC387=2GB/Pink, MC303=4GB/Stainless Steel, "
     "MC307=4GB/Green, MC328=4GB/Blue, MC331=4GB/Pink"),
    ("iPod Shuffle", "4th Gen",
     "MC584=2GB/Silver, MC585=2GB/Pink, MC749=2GB/Orange, MC750=2GB/Green, "
     "MC751=2GB/Blue, MD773=2GB/Pink, MD774=2GB/Yellow, MD775=2GB/Blue, "
     "MD776=2GB/Green, MD777=2GB/Purple, MD778=2GB/Silver, MD779=2GB/Slate, "
     "MD780=2GB/Red, ME949=2GB/Space Gray, MKM72=2GB/Pink, MKM92=2GB/Gold, "
     "MKME2=2GB/Blue, MKMG2=2GB/Silver, MKMJ2=2GB/Space Gray, MKML2=2GB/Red"),
)


def _build_models() -> dict[str, tuple[str, str, str, str]]:
    models: dict[str, tuple[str, str, str, str]] = {}
    for family, generation, rows in _MODEL_GROUPS:
        for cell in rows.split(","):
            number, spec = cell.strip().split("=")
            capacity, color = spec.split("/", 1)
            models[number] = (family, generation, capacity, color)
    return models


IPOD_MODELS: dict[str, tuple[str, str, str, str]] = _build_models()

# Apple (0x05AC) USB product id -> (family, generation); "" generation = coarse PID.
USB_PID_TO_MODEL: dict[int, tuple[str, str]] = {
    0x1201: ("iPod", "3rd Gen"),
    0x1202: ("iPod", ""),
    0x1203: ("iPod", "4th Gen (mono)"),
    0x1204: ("iPod", "4th Gen (photo)"),
    0x1205: ("iPod Mini", ""),
    0x1206: ("iPod", ""),
    0x1207: ("iPod", ""),
    0x1208: ("iPod", ""),
    0x1209: ("iPod", ""),
    0x120A: ("iPod Nano", ""),
    0x1220: ("iPod Nano", "2nd Gen"),
    0x1223: ("iPod", ""),
    0x1224: ("iPod Nano", "3rd Gen"),
    0x1225: ("iPod Nano", "4th Gen"),
    0x1231: ("iPod Nano", "5th Gen"),
    0x1232: ("iPod Nano", "6th Gen"),
    0x1233: ("iPod Shuffle", "4th Gen"),
    0x1234: ("iPod Nano", "7th Gen"),
    0x1240: ("iPod Nano", "2nd Gen"),
    0x1241: ("iPod Classic", "6th Gen"),
    0x1242: ("iPod Nano", "3rd Gen"),
    0x1243: ("iPod Nano", "4th Gen"),
    0x1245: ("iPod Classic", "6.5th Gen"),
    0x1246: ("iPod Nano", "5th Gen"),
    0x1247: ("iPod Classic", "7th Gen"),
    0x1248: ("iPod Nano", "6th Gen"),
    0x1249: ("iPod Nano", "7th Gen"),
    0x124A: ("iPod Nano", "7th Gen"),
    0x1255: ("iPod Nano", "4th Gen"),
    0x1260: ("iPod Nano", "2nd Gen"),
    0x1261: ("iPod Classic", ""),
    0x1262: ("iPod Nano", "3rd Gen"),
    0x1263: ("iPod Nano", "4th Gen"),
    0x1265: ("iPod Nano", "5th Gen"),
    0x1266: ("iPod Nano", "6th Gen"),
    0x1267: ("iPod Nano", "7th Gen"),
    0x1300: ("iPod Shuffle", "1st Gen"),
    0x1301: ("iPod Shuffle", "2nd Gen"),
    0x1302: ("iPod Shuffle", "3rd Gen"),
    0x1303: ("iPod Shuffle", "4th Gen"),
}

IPOD_RECOVERY_USB_PIDS: frozenset[int] = frozenset({
    0x1220, 0x1223, 0x1224, 0x1225, 0x1231, 0x1232, 0x1233, 0x1234,
    0x1240, 0x1241, 0x1242, 0x1243, 0x1245, 0x1246, 0x1247, 0x1248,
    0x1249, 0x124A, 0x1255,
})

IPOD_USB_PIDS: frozenset[int] = frozenset(USB_PID_TO_MODEL)

# model -> serial suffixes, in table order.
_SUFFIX_ROWS: tuple[tuple[str, str], ...] = (
    ("MB029", "Y5N"), ("MB147", "YMV"), ("MB145", "YMU"), ("MB150", "YMX"),
    ("MB562", "2C5"), ("MB565", "2C7"), ("MC293", "9ZS"), ("MC297", "9ZU"),
    ("M8541", "LG6 NAM MJ2"), ("M8709", "ML1 MME"), ("M8737", "MMB"), ("M8738", "MMC"),
    ("M8740", "NGE NGH"), ("M8741", "MMF"), ("M8946", "NLW"), ("M8976", "NRH"),
    ("M9460", "QQF"), ("M9244", "PQ5 PNT"), ("M8948", "NLY NM7"), ("M9245", "PNU"),
    ("M9282", "PS9 Q8U"), ("M9268", "PQ7"), ("M9787", "S2X"), ("MA079", "TDU TDS"),
    ("MA127", "TM2"), ("MA215", "U5H"), ("M9830", "SAZ SB1"), ("M9829", "SAY"),
    ("M9585", "R5Q"), ("M9586", "R5R R5T"), ("M9160", "PFW PRC"), ("M9436", "QKL QKQ"),
    ("M9435", "QKK QKP"), ("M9434", "QKJ QKN"), ("M9437", "QKM QKR"), ("M9800", "S41 S4C"),
    ("M9802", "S43"), ("M9804", "S45"), ("M9805", "S4G S4H"), ("M9806", "S47 S4J"),
    ("M9801", "S42"), ("M9803", "S44"), ("M9807", "S48"),
    ("M9724", "RS9 QGV TSX PFV R80"), ("M9725", "RSA TSY C60"), ("MA546", "VTE VTF"),
    ("MA947", "XQ5 XQS"), ("MA949", "XQV XQX"), ("MB227", "YX7 YXH"), ("MA951", "XQY YX8"),
    ("MA953", "XR1"), ("MB233", "YXA YXL"), ("MB225", "YX6 YX9"), ("MB229", "YXJ"),
    ("MB231", "YXK"), ("MC167", "8CQ"), ("MB518", "1ZH"), ("MB520", "1ZK"),
    ("MB522", "1ZM"), ("MB524", "1ZP"), ("MB526", "1ZR"), ("MB811", "436"),
    ("MB681", "3FK"), ("MB813", "437"), ("MB683", "3FL"), ("MB815", "438"),
    ("MB685", "3FM"), ("MB817", "439"), ("MB779", "3W6"), ("MC306", "A1S"),
    ("MC323", "A78"), ("MC381", "ALB"), ("MC384", "ALD"), ("MC387", "ALG"),
    ("MB867", "4NZ"), ("MC164", "891"), ("MC303", "A1L"), ("MC307", "A1U"),
    ("MC328", "A7B"), ("MC331", "A7D"), ("MC584", "DCMJ"), ("MC585", "DCMK"),
    ("MC749", "DFDM"), ("MC750", "DFDN"), ("MC751", "DFDP"), ("MD773", "F4RT"),
    ("MD774", "F4RV"), ("MD775", "F4RW"), ("MD776", "F4RY"), ("MD777", "F4T0"),
    ("MD778", "F4T1"), ("MD779", "F4VF"), ("MD780", "F4VG"), ("ME949", "FJDH"),
    ("MKM72", "GK67"), ("MKM92", "GK68"), ("MKME2", "GK69"), ("MKMG2", "GK6C"),
    ("MKMJ2", "GK6D"), ("MKML2", "GK6F"), ("MA004", "TUZ SZB SZV SZW"),
    ("MA005", "TV0 SZC SZT"), ("MA099", "TUY TJT TJU"), ("MA107", "TV1 TK2 TK3"),
    ("MA350", "UYN UNA UNB"), ("MA352", "UYP UPR UPS"), ("MA477", "VQ5 VQ6"),
    ("MA426", "V8T V8U"), ("MA428", "V8W V8X"), ("MA487", "VQH VQJ"),
    ("MA489", "VQK VQL VKL"), ("MA725", "WL2 WL3"), ("MA726", "X9A X9B"),
    ("MA497", "VQT VQU"), ("MA899", "YER YES"), ("MA978", "Y0P"), ("MA980", "Y0R"),
    ("MB249", "YXR"), ("MB257", "YXV"), ("MB253", "YXT"), ("MB261", "YXX"),
    ("MB453", "13F"), ("MB663", "37P"), ("MB666", "37Q"), ("MB651", "37G"),
    ("MB654", "37H"), ("MB480", "1P1"), ("MB657", "37K"), ("MB660", "37L"),
    ("MB598", "2ME"), ("MB732", "3QS"), ("MB735", "3QT"), ("MB739", "3QU"),
    ("MB742", "3QW"), ("MB745", "3QX"), ("MB748", "3QY"), ("MB754", "3R0"),
    ("MB751", "3QZ"), ("MB903", "5B7"), ("MB905", "5B8"), ("MB907", "5B9"),
    ("MB909", "5BA"), ("MB911", "5BB"), ("MB913", "5BC"), ("MB915", "5BD"),
    ("MB917", "5BE"), ("MB918", "5BF"), ("MC027", "71V"), ("MC031", "71Y"),
    ("MC034", "721"), ("MC037", "726"), ("MC040", "72A"), ("MC043", "72D"),
    ("MC046", "72F"), ("MC049", "72K"), ("MC050", "72L"), ("MC060", "72Q"),
    ("MC062", "72R"), ("MC064", "72S"), ("MC066", "72X"), ("MC068", "734"),
    ("MC070", "738"), ("MC072", "739"), ("MC074", "73A"), ("MC075", "73B"),
    ("MC525", "DCMN"), ("MC526", "DCMP"), ("MC688", "DDVX"), ("MC689", "DDVY"),
    ("MC690", "DDW0"), ("MC691", "DDW1"), ("MC692", "DDW2"), ("MC693", "DDW3"),
    ("MC694", "DDW4"), ("MC695", "DDW5"), ("MC696", "DDW6"), ("MC697", "DDW7"),
    ("MC698", "DDW8"), ("MC699", "DDW9"), ("MD475", "F0GD F0GM"), ("MD476", "F0GF F0GN"),
    ("MD477", "F0GG F0GP"), ("MD478", "F0GH F0GQ"), ("MD479", "F0GJ F0GR"),
    ("MD480", "F0GK F0GT"), ("MD481", "F0GL F0GV"), ("MD744", "F4LN F4LP"),
    ("ME971", "FJQ1"), ("MKMV2", "GK60"), ("MKMX2", "GK61"), ("MKN02", "GK62"),
    ("MKN22", "GK63"), ("MKN52", "GK64"), ("MKN72", "GK65"),
    ("MA002", "SZ9 WEC WED WEG WEH WEL"), ("MA146", "TXK TXM WEF WEJ WEK"),
    ("MA003", "SZA SZU"), ("MA147", "TXL TXN"), ("MA452", "V9V"),
    ("MA444", "V9K V9L WU9"), ("MA446", "VQM V9M V9N WEE"), ("MA448", "V9P V9Q"),
    ("MA450", "V9R V9S V95 V96 WUC"), ("MA664", "W9G WEM"),
)


def _build_suffixes() -> dict[str, str]:
    table: dict[str, str] = {}
    for model, suffixes in _SUFFIX_ROWS:
        for suffix in suffixes.split():
            table[suffix] = model
    return table


SERIAL_SUFFIX_TO_MODEL: dict[str, str] = _build_suffixes()
SERIAL_LAST3_TO_MODEL = SERIAL_SUFFIX_TO_MODEL


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).casefold()


_CLASSIC_GENERATIONS = {
    "6th gen": "6th Gen",
    "6.5 gen": "6.5th Gen",
    "6.5th gen": "6.5th Gen",
    "7th gen": "7th Gen",
}
_IPOD_GENERATIONS = {
    "4th gen": "4th Gen (mono)",
    "4th gen mono": "4th Gen (mono)",
    "4th gen (mono)": "4th Gen (mono)",
    "4th gen photo": "4th Gen (photo)",
    "4th gen (photo)": "4th Gen (photo)",
    "4th gen color": "4th Gen (color)",
    "4th gen (color)": "4th Gen (color)",
    "5.5 gen": "5.5th Gen",
    "5.5th gen": "5.5th Gen",
}
_FAMILY_NAMES = {
    "ipod nano": "iPod Nano",
    "ipod mini": "iPod Mini",
    "ipod shuffle": "iPod Shuffle",
}


def canonicalize_model_identity(
    family: str,
    generation: str,
    *,
    capacity: str = "",
    color: str = "",
    model_number: str | None = None,
) -> tuple[str, str, str]:
    if model_number:
        number = str(model_number).strip().upper()
        row = IPOD_MODELS.get(number)
        if row is None and number:
            row = IPOD_MODELS.get("M" + number[1:])
        if row is not None:
            return row[0], row[1], row[3]

    family_text = str(family or "").strip()
    generation_text = str(generation or "").strip()
    family_key = _norm(family_text)
    generation_key = _norm(generation_text)
    color_text = str(color or "").strip()

    if family_key == "ipod classic":
        return "iPod Classic", _CLASSIC_GENERATIONS.get(generation_key, generation_text), color_text
    if family_key in _FAMILY_NAMES:
        return _FAMILY_NAMES[family_key], generation_text, color_text
    if family_key == "ipod":
        return "iPod", _IPOD_GENERATIONS.get(generation_key, generation_text), color_text
    return family_text, generation_text, color_text
