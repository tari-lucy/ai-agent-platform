from la_support import make_world, residue, run

MARK = "Лазурный-якорь-Т7"


def test_marker_is_in_source_index_and_report_before_deletion(tmp_path):
    async def scenario():
        world = await make_world(tmp_path)
        lab = await world.lab(0, MARK)
        body = await world.download(lab)
        hears = await world.hears(lab, "костра")
        found = residue(world, MARK)
        await world.close()
        return body, hears, found

    body, hears, found = run(scenario())
    assert MARK.encode() in body and hears
    assert len(found) >= 2  # an index file (or its WAL) and the report body
