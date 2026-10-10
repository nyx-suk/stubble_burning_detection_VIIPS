from app.services.fire_fusion import fusion_confidence


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
