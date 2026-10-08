"""QuicStudio collection domain models.

Each module focuses on a sub-domain; `data.database` imports this package at the
end of the file so that `Base.metadata` and string references in mappers can resolve to these classes.
"""

from data.models.annotation_work import (
    AnnotationSubmission,
    AnnotationSubmissionReview,
    AnnotationWorkItem,
    ReviewWorkItem,
)
from data.models.catalog_dataset import (
    CatalogDataset,
    CatalogDatasetExport,
    CatalogDatasetVersion,
    CatalogDatasetVersionAsset,
)
from data.models.collection_upload import (
    CollectionUploadSession,
    CollectionUploadSessionPackage,
)
from data.models.data_asset import DataAsset
from data.models.episode_admission import EpisodeAdmissionFact
from data.models.native_lerobot_direct import (
    NativeLerobotDirectObject,
    NativeLerobotDirectSource,
)

__all__ = [
    "AnnotationSubmission",
    "AnnotationSubmissionReview",
    "AnnotationWorkItem",
    "CatalogDataset",
    "CatalogDatasetExport",
    "CatalogDatasetVersion",
    "CatalogDatasetVersionAsset",
    "CollectionUploadSession",
    "CollectionUploadSessionPackage",
    "DataAsset",
    "EpisodeAdmissionFact",
    "NativeLerobotDirectObject",
    "NativeLerobotDirectSource",
    "ReviewWorkItem",
]
