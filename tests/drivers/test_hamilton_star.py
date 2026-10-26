"""Hamilton STAR driver against a fake firmware, checked against the command strings PyLabRobot's tests expect."""

from __future__ import annotations

import pytest

from fake_star import FakeStar
from pyreagent.core import DeviceHub, InvalidArgument, NeedsAttention
from pyreagent.devices.liquid_handler import LiquidHandler
from pyreagent.devices.liquid_handler.hamilton import HamiltonStarBackend
from pyreagent.io import MockUsbTransport
from pyreagent.labware import DEFAULT_CATALOG, Labware, LabwareGeometry
from pyreagent.vendors.hamilton.firmware import channel_list, fields, module_errors

# PyLabRobot's STARlet test deck: TIP_CAR_480_A00 on track 1, PLT_CAR_L5AC_A00 on track 9, and its
# Corning 360 µL plate geometry (A1 centre, cavity bottom 3.53 mm above a 0.5 mm well floor).
DEFAULT_CATALOG.add(Labware(
    id="test_cor_96_wellplate_360ul", kind="plate", rows=8, columns=12, well_volume_ul=360,
    geometry=LabwareGeometry(size_x_mm=127.76, size_y_mm=85.48, size_z_mm=14.2, a1_x_mm=14.3, a1_y_mm=74.2, well_bottom_z_mm=3.53,
                             well_floor_mm=0.5, well_depth_mm=10.67, well_diameter_mm=6.86),
))
CARRIERS = {"tips": {"type": "TIP_CAR_480_A00", "track": 1}, "plates": {"type": "PLT_CAR_L5AC_A00", "track": 9}}
DECK = {"tips.2": "hamilton_96_tiprack_300ul_filter", "tips.3": "hamilton_96_tiprack_1000ul_filter",
        "plates.1": {"labware": "test_cor_96_wellplate_360ul", "initial_volume_ul": 300}}
SINGLE = {"style": "single"}


async def start(fake: FakeStar, **options) -> DeviceHub:
    options = {"bottom_clearance_mm": 0.0, **options}
    backend = HamiltonStarBackend(carriers=CARRIERS, transport=MockUsbTransport(fake), **options)
    hub = DeviceHub([LiquidHandler("star", backend, deck=DECK)])
    await hub.start()
    return hub


def test_text_protocol():
    assert channel_list({0: "1"}, 8) == ["1", "1"]
    assert channel_list({0: "1", 1: "1"}, 8) == ["1", "1", "1"]
    assert len(channel_list({ch: "1" for ch in range(8)}, 8)) == 8
    assert module_errors("C0TPid0002er00/00") == {}
    assert module_errors("C0ASid0003er99/00 P106/70 P200/00") == {"P1": (6, 70)}  # the master's 99 just points at P1
    assert module_errors("C0TPid0004er08/76") == {"C0": (8, 76)}
    assert module_errors("P2ZLid0005er70") == {"P2": (0, 70)}
    assert fields("C0RTid0006er00/00rt0 1 0") == {"id": "0006", "rt": "0 1 0"}


async def test_initialisation():
    fake = FakeStar(initialised=False, tips_on=True)
    hub = await start(fake)
    description = hub.describe_device("star")
    assert description["state"] == "idle", description["last_error"]
    assert fake.sent()[:4] == ["C0RM", "C0QM", "C0QW", "C0VI"]
    assert "C0DIxp13400 13400 13400 13400 13400 13400 13400 13400yp4050 3783 3516 3249 2982 2715 2448 2181tp2450tz1220te3600" \
           "tm1 1 1 1 1 1 1 1tt04ti0" in fake.sent()
    assert fake.sent("R0", "C0FI", "C0PG", "H0") == ["R0QW", "C0FI", "C0PGth2800", "H0QW"]
    assert "C0EIxs00420xd1yh1203za2164ze2450" in fake.sent()
    assert {"gripper", "liquid_probe"} <= set(description["limits"]["features"])
    assert [p["name"] for p in description["limits"]["pipettes"]] == ["channels", "core96"]
    await hub.stop()


async def test_wrong_model_is_refused():
    hub = await start(FakeStar(tracks=54))
    assert hub.describe_device("star")["state"] != "idle"
    assert "configured as starlet" in hub.describe_device("star")["last_error"]["message"]
    await hub.stop()


async def test_golden_tips():
    fake = FakeStar()
    hub = await start(fake)
    fake.commands.clear()
    await hub.invoke("star", "pick_up_tip", {"pipette": "channels", "slot": "tips.2", "well": "B1",
                                             "nozzles": {"style": "partial_column", "nozzles": 2}})
    assert fake.sent() == ["C0TTtt01tf1tl0519tv03600tg2tu0",
                           "C0TPxp01179 01179 00000&yp2418 2328 0000&tm1 1 0&tt01tp2244tz2164th2450td0"]
    fake.commands.clear()
    await hub.invoke("star", "return_tip", {"pipette": "channels"})
    assert fake.sent() == ["C0TRxp01179 01179 00000&yp2418 2328 0000&tm1 1 0&tp2244tz2164th2450te2450ti1"]

    await hub.invoke("star", "pick_up_tip", {"pipette": "channels", "slot": "tips.2", "well": "A2", "nozzles": {"style": "all"}})
    fake.commands.clear()
    await hub.invoke("star", "drop_tip", {"pipette": "channels"})
    assert fake.sent() == ["C0TRxp08000 08000 08000 08000 08000 08000 08000 08000yp3427 3337 3247 3157 3067 2977 2887 2797"
                           "tm1 1 1 1 1 1 1 1tp1970tz1870th2450te2450ti0"]
    await hub.stop()


AS_GOLDEN = ("C0ASat0 0&tm1 0&xp02983 00000&yp1457 0000&th2450te2450lp2000 2000&ch000 000&zl1866 1866&po0100 0100&zu0032 0032&"
             "zr06180 06180&zx1866 1866&ip0000 0000&it0 0&fp0000 0000&av01072 01072&as1000 1000&ta000 000&ba0000 0000&oa000 000&"
             "lm0 0&ll1 1&lv1 1&zo000 000&ld00 00&de0020 0020&wt10 10&mv00000 00000&mc00 00&mp000 000&ms1000 1000&mh0000 0000&"
             "gi000 000&gj0gk0lk0 0&ik0000 0000&sd0500 0500&se0500 0500&sz0300 0300&io0000 0000&")
DS_GOLDEN = ("C0DSdm1 1&tm1 0&xp02983 00000&yp1457 0000&zx1866 1866&lp2000 2000&zl1866 1866&po0100 0100&ip0000 0000&it0 0&"
             "fp0000 0000&zu0032 0032&zr06180 06180&th2450te2450dv01072 01072&ds1800 1800&ss0050 0050&rv000 000&ta050 050&"
             "ba0300 0300&lm0 0&dj00zo000 000&ll1 1&lv1 1&de0010 0010&wt00 00&mv00000 00000&mc00 00&mp000 000&ms0010 0010&"
             "mh0000 0000&gi000 000&gj0gk0")


async def test_golden_aspirate():
    fake = FakeStar()
    hub = await start(fake, blow_out=False)
    await hub.invoke("star", "pick_up_tip", {"pipette": "channels", "slot": "tips.2", "well": "A1", "nozzles": SINGLE})
    fake.commands.clear()
    await hub.invoke("star", "aspirate", {"pipette": "channels", "slot": "plates.1", "well": "A1", "volume_ul": 100})
    assert fake.sent() == [AS_GOLDEN]
    await hub.stop()


async def test_golden_dispense():
    fake = FakeStar()
    hub = await start(fake, dispense_mode="jet", blow_out=True)
    await hub.invoke("star", "pick_up_tip", {"pipette": "channels", "slot": "tips.2", "well": "A1", "nozzles": SINGLE})
    await hub.invoke("star", "aspirate", {"pipette": "channels", "slot": "plates.1", "well": "A1", "volume_ul": 100})
    assert "ba0300 0300&" in fake.sent("C0AS")[0]  # blow-out air taken up with the liquid
    fake.commands.clear()
    await hub.invoke("star", "dispense", {"pipette": "channels", "slot": "plates.1", "well": "A1", "volume_ul": 100})
    assert fake.sent() == [DS_GOLDEN]
    await hub.stop()


async def test_partial_dispense_keeps_blow_out_air():
    fake = FakeStar()
    hub = await start(fake)
    await hub.invoke("star", "pick_up_tip", {"pipette": "channels", "slot": "tips.2", "well": "A1", "nozzles": SINGLE})
    await hub.invoke("star", "aspirate", {"pipette": "channels", "slot": "plates.1", "well": "A1", "volume_ul": 200})
    await hub.invoke("star", "dispense", {"pipette": "channels", "slot": "plates.1", "well": "A1", "volume_ul": 100})
    await hub.invoke("star", "dispense", {"pipette": "channels", "slot": "plates.1", "well": "A1", "volume_ul": 100})
    first, last = fake.sent("C0DS")
    assert first.startswith("C0DSdm2 2&") and "dv01072 01072&" in first   # surface, partial
    assert last.startswith("C0DSdm3 3&") and "dv01038 01038&" in last     # surface, empty: what is left (211.0 - 107.2)
    await hub.stop()


async def test_golden_core96():
    fake = FakeStar()
    hub = await start(fake)
    fake.commands.clear()
    await hub.invoke("star", "pick_up_tip", {"pipette": "core96", "slot": "tips.2"})
    assert fake.sent() == ["C0TTtt01tf1tl0519tv03600tg2tu0", "H0DQdq11281dv13500du00000dr900000dw15",
                           "C0EPxs01179xd0yh2418tt01wu0za2164zh2450ze2450"]
    fake.commands.clear()
    await hub.invoke("star", "return_tip", {"pipette": "core96"})
    assert fake.sent() == ["C0ERxs01179xd0yh2418za2164zh2450ze2450"]

    await hub.invoke("star", "pick_up_tip", {"pipette": "core96", "slot": "tips.3"})
    fake.commands.clear()
    await hub.invoke("star", "aspirate", {"pipette": "core96", "slot": "plates.1", "well": "A1", "volume_ul": 100})
    await hub.invoke("star", "dispense", {"pipette": "core96", "slot": "plates.1", "well": "A1", "volume_ul": 100})
    aspirate, dispense = fake.sent()
    # PyLabRobot sends a fixed LLD search height of 199.9 mm and 5 µL pre-wetting; pyreagent searches from 2.7 mm above
    # the well (200.0 mm here) and does not pre-wet.
    assert aspirate == ("C0EAaa0xs02983xd0yh1457zh2450ze2450lz2000zt1866pp0100zm1866zv0032zq06180iw000ix0fh000af01083ag2500vt050"
                        "bv00000wv00000cm0cs1bs0020wh10hv00000hc00hp000mj000hs1200cwFFFFFFFFFFFFFFFFFFFFFFFFcr000cj0cx0")
    assert dispense == ("C0EDda3xs02983xd0yh1457zm1866zv0032zq06180lz2000zt1866pp0100iw000ix0fh000zh2450ze2450df01083dg1200es0050"
                        "ev000vt050bv00000cm0cs1ej00bs0020wh00hv00000hc00hp000mj000hs1200cwFFFFFFFFFFFFFFFFFFFFFFFFcr000cj0cx0")
    fake.commands.clear()
    await hub.invoke("star", "drop_tip", {"pipette": "core96"})
    assert fake.sent() == ["C0ERxs00420xd1yh1203za2164zh2450ze2450"]
    await hub.stop()


async def test_golden_iswap():
    fake = FakeStar()
    hub = await start(fake, iswap_grip_from_top_mm=13.2 - 3.33)
    fake.commands.clear()
    job = await hub.jobs.wait((await hub.invoke("star", "move_labware", {"from_slot": "plates.1", "to_slot": "plates.3"})).job.id, 5)
    assert job.status == "succeeded", job.error
    assert fake.sent() == ["C0PPxs03479xd0yj1142yd0zj1874zd0gr1th2800te2800gw4go1308gb1245gt20ga0gc0",
                           "C0PRxs03479xd0yj3062yd0zj1874zd0th2800te2800gr1go1308ga0gc0", "C0PGth2800"]
    await hub.stop()


async def probe(hub: DeviceHub, wells: list[str]) -> dict:
    job = (await hub.invoke("star", "probe_liquid_heights", {"pipette": "channels", "slot": "plates.1", "wells": wells})).job
    job = await hub.jobs.wait(job.id, 5)
    assert job.status == "succeeded", job.error
    return job.result


async def test_probe_liquid_heights():
    fake = FakeStar()
    hub = await start(fake)
    await hub.invoke("star", "pick_up_tip", {"pipette": "channels", "slot": "tips.2", "well": "A3", "nozzles": SINGLE})
    fake.liquid_levels = [[1966] + [0] * 7, [1916] + [0] * 7]
    fake.commands.clear()
    heights = (await probe(hub, ["A1", "B1"]))["heights_mm"]
    assert heights["A1"] == pytest.approx(10.0, abs=0.06) and heights["B1"] == pytest.approx(5.0, abs=0.06)
    sent = fake.sent()
    assert sent[:3] == ["C0ZA", "C0KXxs02983", "C0JYyp1457 1367 1277 1187 1097 1007 0917 0827"]
    assert [c[:4] for c in sent[3:]] == ["P1ZL", "C0RL", "C0ZA", "C0KX", "C0JY", "P1ZL", "C0RL", "C0ZA"]
    # search from 5 mm above the well down to its bottom, in Z-drive steps of the bare channel (tip length - 8 mm above the tip end)
    assert sent[3] == "P1ZLzh22237zc23698zl00932zr075gt0010gl0002zj1zi0186"

    fake.liquid_levels = []
    fake.fail["P1ZL"] = "er70"
    assert (await probe(hub, ["C1"]))["heights_mm"] == {"C1": None}
    await hub.stop()


async def test_firmware_errors_become_structured():
    fake = FakeStar()
    hub = await start(fake)
    await hub.invoke("star", "pick_up_tip", {"pipette": "channels", "slot": "tips.2", "well": "A1", "nozzles": SINGLE})
    fake.fail["C0AS"] = "er99/00 P106/70"
    with pytest.raises(NeedsAttention, match="no liquid level found"):
        await hub.invoke("star", "aspirate", {"pipette": "channels", "slot": "plates.1", "well": "A1", "volume_ul": 50})
    await hub.stop()

    fake = FakeStar()
    hub = await start(fake)
    fake.fail["C0TP"] = "er99/00 P108/75"
    with pytest.raises(NeedsAttention, match="no tip picked up"):
        await hub.invoke("star", "pick_up_tip", {"pipette": "channels", "slot": "tips.2", "well": "A1", "nozzles": SINGLE})
    await hub.stop()


async def test_deck_checks():
    hub = await start(FakeStar())
    with pytest.raises(InvalidArgument, match="not a Hamilton tip rack"):
        await hub.invoke("star", "load_labware", {"slot": "tips.4", "labware": "opentrons_96_tiprack_300ul"})
    with pytest.raises(InvalidArgument, match="plate carrier"):
        await hub.invoke("star", "load_labware", {"slot": "plates.4", "labware": "hamilton_96_tiprack_50ul"})
    with pytest.raises(InvalidArgument, match="No deck slot"):
        await hub.invoke("star", "load_labware", {"slot": "plates.9", "labware": "test_cor_96_wellplate_360ul"})
    await hub.stop()
