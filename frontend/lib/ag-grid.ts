'use client';

import { AllCommunityModule, ModuleRegistry, themeQuartz } from 'ag-grid-community';
import { CellSelectionModule, ClipboardModule, ColumnMenuModule, ContextMenuModule, ExcelExportModule, LicenseManager, RichSelectModule, ServerSideRowModelApiModule,
  ServerSideRowModelModule } from 'ag-grid-enterprise';
import { AG_GRID_LOCALE_ES } from '@ag-grid-community/locale';

const license = process.env.NEXT_PUBLIC_AG_GRID_LICENSE_KEY;
if (license) LicenseManager.setLicenseKey(license);
ModuleRegistry.registerModules([AllCommunityModule, CellSelectionModule, ClipboardModule, ColumnMenuModule, ContextMenuModule, ExcelExportModule,
  RichSelectModule, ServerSideRowModelModule, ServerSideRowModelApiModule]);

export const gridLocale = AG_GRID_LOCALE_ES;
export const motionGridTheme = themeQuartz.withParams({
  accentColor: '#6e954b', backgroundColor: '#ffffff', foregroundColor: '#344331',
  borderColor: '#dfe5d8', headerBackgroundColor: '#f1f5eb', headerTextColor: '#647858',
  fontFamily: 'DM Sans, sans-serif', fontSize: 12, headerFontSize: 11,
  spacing: 6, rowHeight: 56, headerHeight: 42, wrapperBorderRadius: 10,
});
