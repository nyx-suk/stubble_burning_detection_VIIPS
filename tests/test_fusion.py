from app.services.fire_fusion import fusion_confidence, lance_quality_valid, _deduplicate


def test_source_consensus_and_persistence_raise_fusion_confidence():
    factors, confidence = fusion_confidence(
        frp=6,
        persistence=3,
        has_consensus=True,
        temperature_validated=False,
    )

    assert factors == ["High FRP", "High Persistence", "Multi-Source Consensus"]
    assert confidence == "Very High"


def test_single_low_confidence_detection_stays_low():
    factors, confidence = fusion_confidence(
        frp=0.2,
        persistence=1,
        has_consensus=False,
        temperature_validated=False,
    )

    assert factors == ["Low FRP"]
    assert confidence == "Low"


def test_lance_quality_screen_requires_frp_confidence_and_clear_quality():
    assert lance_quality_valid(0.11, 30, True)
    assert not lance_quality_valid(0.1, 80, True)
    assert not lance_quality_valid(2, 29, True)
    assert not lance_quality_valid(2, 50, False)
    assert not lance_quality_valid(None, 50, True)


def test_fusion_deduplicates_close_sources_but_preserves_native_scales():
    features = [
        {"latitude":30.0,"longitude":75.0,"frp":2,"confidence":"Nominal","source":"LANCE","native_resolution_m":375},
        {"latitude":30.002,"longitude":75.002,"frp":4,"confidence":"High","source":"VNP14A1","native_resolution_m":1000},
        {"latitude":31.0,"longitude":76.0,"frp":0,"confidence":"Low","source":"LANCE","native_resolution_m":375},
    ]
    result = _deduplicate(features)
    assert len(result) == 2
    fused = next(row for row in result if row["latitude"] == 30.0)
    assert fused["source"] == "LANCE, VNP14A1"
    assert fused["native_resolutions_m"] == [375, 1000]
    assert fused["corroboration"] == "Spatially corroborated within query period"


def test_fusion_empty_dataset_is_empty():
    assert _deduplicate([]) == []
