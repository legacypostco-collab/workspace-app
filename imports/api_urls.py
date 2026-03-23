from django.urls import path

from .api import (
    SupplierImportDetailAPIView,
    SupplierImportErrorsDownloadAPIView,
    SupplierImportFileCreateAPIView,
    SupplierImportGoogleSheetCreateAPIView,
    SupplierImportListAPIView,
    SupplierImportPreviewConfirmMappingAPIView,
    SupplierImportPreviewDetailAPIView,
    SupplierImportProgressAPIView,
    SupplierImportRollbackAPIView,
    SupplierImportRowsAPIView,
    SupplierImportStartAPIView,
)

urlpatterns = [
    path("seller/imports/upload", SupplierImportFileCreateAPIView.as_view(), name="seller_import_upload"),
    path("seller/imports/file", SupplierImportFileCreateAPIView.as_view(), name="seller_import_file_alias"),
    path("seller/imports/google-sheet", SupplierImportGoogleSheetCreateAPIView.as_view(), name="seller_import_google_sheet"),
    path("seller/imports/preview/<int:preview_id>", SupplierImportPreviewDetailAPIView.as_view(), name="seller_import_preview_detail"),
    path(
        "seller/imports/preview/<int:preview_id>/confirm-mapping",
        SupplierImportPreviewConfirmMappingAPIView.as_view(),
        name="seller_import_preview_confirm_mapping",
    ),
    path("seller/imports/start", SupplierImportStartAPIView.as_view(), name="seller_import_start"),
    path("seller/imports", SupplierImportListAPIView.as_view(), name="seller_import_list"),
    path("seller/imports/<int:import_id>", SupplierImportDetailAPIView.as_view(), name="seller_import_detail"),
    path("seller/imports/<int:import_id>/progress", SupplierImportProgressAPIView.as_view(), name="seller_import_progress"),
    path("seller/imports/<int:import_id>/rows", SupplierImportRowsAPIView.as_view(), name="seller_import_rows"),
    path("seller/imports/<int:import_id>/rollback", SupplierImportRollbackAPIView.as_view(), name="seller_import_rollback"),
    path("seller/imports/<int:import_id>/errors", SupplierImportErrorsDownloadAPIView.as_view(), name="seller_import_errors_download_alias"),
    path("supplier/imports/file", SupplierImportFileCreateAPIView.as_view(), name="supplier_import_file"),
    path(
        "supplier/imports/google-sheet",
        SupplierImportGoogleSheetCreateAPIView.as_view(),
        name="supplier_import_google_sheet",
    ),
    path(
        "supplier/imports/preview/<int:preview_id>",
        SupplierImportPreviewDetailAPIView.as_view(),
        name="supplier_import_preview_detail",
    ),
    path(
        "supplier/imports/preview/<int:preview_id>/confirm-mapping",
        SupplierImportPreviewConfirmMappingAPIView.as_view(),
        name="supplier_import_preview_confirm_mapping",
    ),
    path("supplier/imports/start", SupplierImportStartAPIView.as_view(), name="supplier_import_start"),
    path("supplier/imports", SupplierImportListAPIView.as_view(), name="supplier_import_list"),
    path("supplier/imports/<int:import_id>", SupplierImportDetailAPIView.as_view(), name="supplier_import_detail"),
    path("supplier/imports/<int:import_id>/progress", SupplierImportProgressAPIView.as_view(), name="supplier_import_progress"),
    path("supplier/imports/<int:import_id>/rows", SupplierImportRowsAPIView.as_view(), name="supplier_import_rows"),
    path("supplier/imports/<int:import_id>/rollback", SupplierImportRollbackAPIView.as_view(), name="supplier_import_rollback"),
    path(
        "supplier/imports/<int:import_id>/errors",
        SupplierImportErrorsDownloadAPIView.as_view(),
        name="supplier_import_errors_download",
    ),
]
