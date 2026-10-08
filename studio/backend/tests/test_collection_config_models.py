"""Collection configuration domain models: label dictionary and device model catalog."""

import pytest
from sqlalchemy.exc import IntegrityError

from data.database import SessionLocal
from data.models.collection_config import (
    COLLECTION_LABEL_CATEGORIES,
    CollectionDeviceModel,
    CollectionLabel,
)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def test_label_categories_cover_the_five_required_kinds():
    assert set(COLLECTION_LABEL_CATEGORIES) == {
        "scene",
        "purpose",
        "training",
        "project",
        "modality",
    }


def test_label_rejects_unknown_category(db):
    db.add(CollectionLabel(category="region", name="华东"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_label_name_is_unique_per_category_ignoring_padding(db):
    db.add(CollectionLabel(category="scene", name="厨房"))
    db.commit()
    db.add(CollectionLabel(category="scene", name="  厨房  "))
    with pytest.raises(IntegrityError):
        db.commit()


def test_same_name_allowed_in_a_different_category(db):
    db.add(CollectionLabel(category="scene", name="通用"))
    db.add(CollectionLabel(category="purpose", name="通用"))
    db.commit()
    assert db.query(CollectionLabel).filter_by(name="通用").count() == 2


def test_label_defaults_to_active(db):
    label = CollectionLabel(category="training", name="预训练")
    db.add(label)
    db.commit()
    assert label.is_active is True


def test_device_model_vendor_model_pair_is_unique(db):
    db.add(CollectionDeviceModel(vendor="Quic", model="EGO-1", device_type="iphone"))
    db.commit()
    db.add(CollectionDeviceModel(vendor="quic", model=" ego-1 ", device_type="iphone"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_device_model_modalities_default_to_empty_list(db):
    entry = CollectionDeviceModel(vendor="Quic", model="UMI-1", device_type="iphone")
    db.add(entry)
    db.commit()
    assert entry.modalities_json == []
