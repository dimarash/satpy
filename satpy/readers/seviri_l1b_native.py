#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2017-2019 Satpy developers
#
# This file is part of satpy.
#
# satpy is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# satpy is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# satpy.  If not, see <http://www.gnu.org/licenses/>.
"""SEVIRI native format reader.

Notes:
    When loading solar channels, this reader applies a correction for the
    Sun-Earth distance variation throughout the year - as recommended by
    the EUMETSAT document:
        'Conversion from radiances to reflectances for SEVIRI warm channels'
    In the unlikely situation that this correction is not required, it can be
    removed on a per-channel basis using the
    satpy.readers.utils.remove_earthsun_distance_correction(channel, utc_time)
    function.

References:
    - `MSG Level 1.5 Native Format File Definition`_
    - `MSG Level 1.5 Image Data Format Description`_
    - `Conversion from radiances to reflectances for SEVIRI warm channels`_

.. _MSG Level 1.5 Native Format File Definition
    https://www-cdn.eumetsat.int/files/2020-04/pdf_fg15_msg-native-format-15.pdf
.. _MSG Level 1.5 Image Data Format Description
    https://www-cdn.eumetsat.int/files/2020-05/pdf_ten_05105_msg_img_data.pdf
.. _Conversion from radiances to reflectances for SEVIRI warm channels:
    https://www-cdn.eumetsat.int/files/2020-04/pdf_msg_seviri_rad2refl.pdf

"""

import logging
from datetime import datetime
import numpy as np

import xarray as xr
import dask.array as da

from satpy import CHUNK_SIZE

from pyresample import geometry

from satpy.readers.file_handlers import BaseFileHandler
from satpy.readers.eum_base import recarray2dict
from satpy.readers.seviri_base import (SEVIRICalibrationHandler,
                                       CHANNEL_NAMES, CALIB, SATNUM,
                                       dec10216, VISIR_NUM_COLUMNS,
                                       VISIR_NUM_LINES, HRV_NUM_COLUMNS, HRV_NUM_LINES,
                                       VIS_CHANNELS, get_service_mode, pad_data_horizontally, pad_data_vertically)
from satpy.readers.seviri_l1b_native_hdr import (GSDTRecords, native_header,
                                                 native_trailer)
from satpy.readers._geos_area import get_area_definition

logger = logging.getLogger('native_msg')


class NativeMSGFileHandler(BaseFileHandler, SEVIRICalibrationHandler):
    """SEVIRI native format reader.

    The Level1.5 Image data calibration method can be changed by adding the
    required mode to the Scene object instantiation  kwargs eg
    kwargs = {"calib_mode": "gsics",}

    **Padding channel data to full disk**

    By providing the `fill_disk` as True in the `reader_kwargs`, the channel is loaded
    as full disk, padded with no-data where necessary. This is especially useful for the
    HRV channel, but can also be used for RSS and ROI data. By default the original,
    unpadded, data are loaded::

        scene = satpy.Scene(filenames,
                            reader='seviri_l1b_native',
                            reader_kwargs={'fill_disk': False})
    """

    def __init__(self, filename, filename_info, filetype_info, calib_mode='nominal', fill_disk=False):
        """Initialize the reader."""
        super(NativeMSGFileHandler, self).__init__(filename,
                                                   filename_info,
                                                   filetype_info)
        self.platform_name = None
        self.calib_mode = calib_mode
        self.fill_disk = fill_disk

        # Declare required variables.
        self.header = {}
        self.mda = {}
        self.trailer = {}

        # Read header, prepare dask-array, read trailer and initialize image boundaries
        # Available channels are known only after the header has been read
        self._read_header()
        self.dask_array = da.from_array(self._get_memmap(), chunks=(CHUNK_SIZE,))
        self._read_trailer()
        self.image_boundaries = ImageBoundaries(self.header, self.trailer, self.mda)

    @property
    def start_time(self):
        """Read the repeat cycle start time from metadata."""
        return self.header['15_DATA_HEADER']['ImageAcquisition'][
            'PlannedAcquisitionTime']['TrueRepeatCycleStart']

    @property
    def end_time(self):
        """Read the repeat cycle end time from metadata."""
        return self.header['15_DATA_HEADER']['ImageAcquisition'][
            'PlannedAcquisitionTime']['PlannedRepeatCycleEnd']

    @staticmethod
    def _calculate_area_extent(center_point, north, east, south, west,
                               we_offset, ns_offset, column_step, line_step):
        # For Earth model 2 and full disk VISIR, (center_point - west - 0.5 + we_offset) must be -1856.5 .
        # See MSG Level 1.5 Image Data Format Description Figure 7 - Alignment and numbering of the non-HRV pixels.

        ll_c = (center_point - east + 0.5 + we_offset) * column_step
        ll_l = (north - center_point + 0.5 + ns_offset) * line_step
        ur_c = (center_point - west - 0.5 + we_offset) * column_step
        ur_l = (south - center_point - 0.5 + ns_offset) * line_step

        return (ll_c, ll_l, ur_c, ur_l)

    def _get_data_dtype(self):
        """Get the dtype of the file based on the actual available channels."""
        pkhrec = [
            ('GP_PK_HEADER', GSDTRecords.gp_pk_header),
            ('GP_PK_SH1', GSDTRecords.gp_pk_sh1)
        ]
        pk_head_dtype = np.dtype(pkhrec)

        def get_lrec(cols):
            lrec = [
                ("gp_pk", pk_head_dtype),
                ("version", np.uint8),
                ("satid", np.uint16),
                ("time", (np.uint16, 5)),
                ("lineno", np.uint32),
                ("chan_id", np.uint8),
                ("acq_time", (np.uint16, 3)),
                ("line_validity", np.uint8),
                ("line_rquality", np.uint8),
                ("line_gquality", np.uint8),
                ("line_data", (np.uint8, cols))
            ]

            return lrec

        # each pixel is 10-bits -> one line of data has 25% more bytes
        # than the number of columns suggest (10/8 = 1.25)
        visir_rec = get_lrec(int(self.mda['number_of_columns'] * 1.25))
        number_of_visir_channels = len(
            [s for s in self.mda['channel_list'] if not s == 'HRV'])
        drec = [('visir', (visir_rec, number_of_visir_channels))]

        if self.mda['available_channels']['HRV']:
            hrv_rec = get_lrec(int(self.mda['hrv_number_of_columns'] * 1.25))
            drec.append(('hrv', (hrv_rec, 3)))

        return np.dtype(drec)

    def _get_memmap(self):
        """Get the memory map for the SEVIRI data."""
        with open(self.filename) as fp:
            data_dtype = self._get_data_dtype()
            hdr_size = native_header.itemsize

            return np.memmap(fp, dtype=data_dtype,
                             shape=(self.mda['number_of_lines'],),
                             offset=hdr_size, mode="r")

    def _read_header(self):
        """Read the header info."""
        data = np.fromfile(self.filename,
                           dtype=native_header, count=1)

        self.header.update(recarray2dict(data))

        data15hd = self.header['15_DATA_HEADER']
        sec15hd = self.header['15_SECONDARY_PRODUCT_HEADER']

        # Set the list of available channels:
        self.mda['available_channels'] = get_available_channels(self.header)
        self.mda['channel_list'] = [i for i in CHANNEL_NAMES.values()
                                    if self.mda['available_channels'][i]]

        self.platform_id = data15hd[
            'SatelliteStatus']['SatelliteDefinition']['SatelliteId']
        self.mda['platform_name'] = "Meteosat-" + SATNUM[self.platform_id]

        equator_radius = data15hd['GeometricProcessing'][
                             'EarthModel']['EquatorialRadius'] * 1000.
        north_polar_radius = data15hd[
                                 'GeometricProcessing']['EarthModel']['NorthPolarRadius'] * 1000.
        south_polar_radius = data15hd[
                                 'GeometricProcessing']['EarthModel']['SouthPolarRadius'] * 1000.
        polar_radius = (north_polar_radius + south_polar_radius) * 0.5
        ssp_lon = data15hd['ImageDescription'][
            'ProjectionDescription']['LongitudeOfSSP']

        self.mda['projection_parameters'] = {'a': equator_radius,
                                             'b': polar_radius,
                                             'h': 35785831.00,
                                             'ssp_longitude': ssp_lon}

        north = int(sec15hd['NorthLineSelectedRectangle']['Value'])
        east = int(sec15hd['EastColumnSelectedRectangle']['Value'])
        south = int(sec15hd['SouthLineSelectedRectangle']['Value'])
        west = int(sec15hd['WestColumnSelectedRectangle']['Value'])

        ncolumns = west - east + 1
        nrows = north - south + 1

        # check if the file has less rows or columns than
        # the maximum, if so it is a rapid scanning service
        # or region of interest file
        if (nrows < VISIR_NUM_LINES) or (ncolumns < VISIR_NUM_COLUMNS):
            self.mda['is_full_disk'] = False
        else:
            self.mda['is_full_disk'] = True

        # If the number of columns in the file is not divisible by 4,
        # UMARF will add extra columns to the file
        modulo = ncolumns % 4
        padding = 0
        if modulo > 0:
            padding = 4 - modulo
        cols_visir = ncolumns + padding

        # Check the VISIR calculated column dimension against
        # the header information
        cols_visir_hdr = int(sec15hd['NumberColumnsVISIR']['Value'])
        if cols_visir_hdr != cols_visir:
            logger.warning(
                "Number of VISIR columns from the header is incorrect!")
            logger.warning("Header: %d", cols_visir_hdr)
            logger.warning("Calculated: = %d", cols_visir)

        # HRV Channel - check if the area is reduced in east west
        # direction as this affects the number of columns in the file
        cols_hrv_hdr = int(sec15hd['NumberColumnsHRV']['Value'])
        if ncolumns < VISIR_NUM_COLUMNS:
            cols_hrv = cols_hrv_hdr
        else:
            cols_hrv = int(cols_hrv_hdr / 2)

        # self.mda represents the 16bit dimensions not 10bit
        self.mda['number_of_lines'] = int(sec15hd['NumberLinesVISIR']['Value'])
        self.mda['number_of_columns'] = cols_visir
        self.mda['hrv_number_of_lines'] = int(sec15hd["NumberLinesHRV"]['Value'])
        self.mda['hrv_number_of_columns'] = cols_hrv

    def _read_trailer(self):

        hdr_size = native_header.itemsize
        data_size = (self._get_data_dtype().itemsize *
                     self.mda['number_of_lines'])

        with open(self.filename) as fp:
            fp.seek(hdr_size + data_size)
            data = np.fromfile(fp, dtype=native_trailer, count=1)

        self.trailer.update(recarray2dict(data))

    def get_area_def(self, dataset_id):
        """Get the area definition of the band.

        In general, image data from one window/area is available. For the HRV channel in FES mode, however,
        data from two windows ('Lower' and 'Upper') are available. Hence, we collect lists of area-extents
        and corresponding number of image lines/columns. In case of FES HRV data, two area definitions are
        computed, stacked and squeezed. For other cases, the lists will only have one entry each, from which
        a single area definition is computed.
        """
        pdict = {}
        pdict['a'] = self.mda['projection_parameters']['a']
        pdict['b'] = self.mda['projection_parameters']['b']
        pdict['h'] = self.mda['projection_parameters']['h']
        pdict['ssp_lon'] = self.mda['projection_parameters']['ssp_longitude']

        if dataset_id['name'] == 'HRV':
            res = 1.0
            pdict['p_id'] = 'seviri_hrv'
        else:
            res = 3.0
            pdict['p_id'] = 'seviri_visir'

        service_mode = get_service_mode(pdict['ssp_lon'])
        pdict['a_name'] = 'msg_seviri_%s_%.0fkm' % (service_mode['name'], res)
        pdict['a_desc'] = 'SEVIRI %s area definition with %.0f km resolution' % (service_mode['desc'], res)

        area_extent = self.get_area_extent(dataset_id)
        areas = list()
        for aex, nlines, ncolumns in zip(area_extent['area_extent'], area_extent['nlines'], area_extent['ncolumns']):
            pdict['nlines'] = nlines
            pdict['ncols'] = ncolumns
            areas.append(get_area_definition(pdict, aex))

        if len(areas) == 2:
            area = geometry.StackedAreaDefinition(areas[0], areas[1])
            area = area.squeeze()
        else:
            area = areas[0]

        return area

    def get_area_extent(self, dataset_id):
        """Get the area extent of the file.

        Until December 2017, the data is shifted by 1.5km SSP North and West against the nominal GEOS projection. Since
        December 2017 this offset has been corrected. A flag in the data indicates if the correction has been applied.
        If no correction was applied, adjust the area extent to match the shifted data.

        For more information see Section 3.1.4.2 in the MSG Level 1.5 Image Data Format Description. The correction
        of the area extent is documented in a `developer's memo <https://github.com/pytroll/satpy/wiki/
        SEVIRI-georeferencing-offset-correction>`_.
        """
        data15hd = self.header['15_DATA_HEADER']

        # check for Earth model as this affects the north-south and
        # west-east offsets
        # section 3.1.4.2 of MSG Level 1.5 Image Data Format Description
        earth_model = data15hd['GeometricProcessing']['EarthModel'][
            'TypeOfEarthModel']
        if earth_model == 2:
            ns_offset = 0
            we_offset = 0
        elif earth_model == 1:
            ns_offset = -0.5
            we_offset = 0.5
            if dataset_id['name'] == 'HRV':
                ns_offset = -1.5
                we_offset = 1.5
        else:
            raise NotImplementedError(
                'Unrecognised Earth model: {}'.format(earth_model)
            )

        if dataset_id['name'] == 'HRV':
            grid_origin = data15hd['ImageDescription']['ReferenceGridHRV']['GridOrigin']
            center_point = (HRV_NUM_COLUMNS / 2) - 2
            column_step = data15hd['ImageDescription']['ReferenceGridHRV']['ColumnDirGridStep'] * 1000.0
            line_step = data15hd['ImageDescription']['ReferenceGridHRV']['LineDirGridStep'] * 1000.0
            nlines_fulldisk = HRV_NUM_LINES
            ncolumns_fulldisk = HRV_NUM_COLUMNS
        else:
            grid_origin = data15hd['ImageDescription']['ReferenceGridVIS_IR']['GridOrigin']
            center_point = VISIR_NUM_COLUMNS / 2
            column_step = data15hd['ImageDescription']['ReferenceGridVIS_IR']['ColumnDirGridStep'] * 1000.0
            line_step = data15hd['ImageDescription']['ReferenceGridVIS_IR']['LineDirGridStep'] * 1000.0
            nlines_fulldisk = VISIR_NUM_LINES
            ncolumns_fulldisk = VISIR_NUM_COLUMNS

        # Calculations assume grid origin is south-east corner
        # section 7.2.4 of MSG Level 1.5 Image Data Format Description
        origins = {0: 'NW', 1: 'SW', 2: 'SE', 3: 'NE'}
        if grid_origin != 2:
            msg = 'Grid origin not supported number: {}, {} corner'.format(
                grid_origin, origins[grid_origin]
            )
            raise NotImplementedError(msg)

        aex_data = {'area_extent': [], 'nlines': [], 'ncolumns': []}

        img_bounds = self.image_boundaries.get_img_bounds(dataset_id, self.is_roi())
        for south_bound, north_bound, east_bound, west_bound in zip(*img_bounds.values()):

            if self.fill_disk:
                east_bound, west_bound = 1, ncolumns_fulldisk
                if not self.mda['is_full_disk']:
                    south_bound, north_bound = 1, nlines_fulldisk

            nlines = north_bound - south_bound + 1
            ncolumns = west_bound - east_bound + 1
            aex = self._calculate_area_extent(center_point, north_bound, east_bound, south_bound, west_bound,
                                              we_offset, ns_offset, column_step, line_step)

            aex_data['area_extent'].append(aex)
            aex_data['nlines'].append(nlines)
            aex_data['ncolumns'].append(ncolumns)

        return aex_data

    def is_roi(self):
        """Check if data covers a selected region of interest (ROI).

        Standard RSS data consists of 3712 columns and 1392 lines, covering the three northmost segements
        of the SEVIRI disk. Hence, if the data does not cover the full disk, nor the standard RSS region
        in RSS mode, it's assumed to be ROI data.
        """
        is_rapid_scan = self.trailer['15TRAILER']['ImageProductionStats']['ActualScanningSummary']['ReducedScan']

        # Standard RSS data is assumed to cover the three northmost segements, thus consisting of all 3712 columns and
        # the 1392 northmost lines
        nlines = int(self.mda['number_of_lines'])
        ncolumns = int(self.mda['number_of_columns'])
        north_bound = int(self.header['15_SECONDARY_PRODUCT_HEADER']['NorthLineSelectedRectangle']['Value'])

        is_top3segments = (ncolumns == VISIR_NUM_COLUMNS and nlines == 1392 and north_bound == VISIR_NUM_LINES)

        return not self.mda['is_full_disk'] and not (is_rapid_scan and is_top3segments)

    def get_dataset(self, dataset_id, dataset_info):
        """Get the dataset."""
        if dataset_id['name'] not in self.mda['channel_list']:
            raise KeyError('Channel % s not available in the file' % dataset_id['name'])
        elif dataset_id['name'] not in ['HRV']:
            data = self._get_visir_channel(dataset_id)
        else:
            data = self._get_hrv_channel()

        xarr = xr.DataArray(data, dims=['y', 'x']).where(data != 0).astype(np.float32)

        if xarr is None:
            return None

        dataset = self.calibrate(xarr, dataset_id)
        dataset.attrs['units'] = dataset_info['units']
        dataset.attrs['wavelength'] = dataset_info['wavelength']
        dataset.attrs['standard_name'] = dataset_info['standard_name']
        dataset.attrs['platform_name'] = self.mda['platform_name']
        dataset.attrs['sensor'] = 'seviri'
        dataset.attrs['orbital_parameters'] = {
            'projection_longitude': self.mda['projection_parameters']['ssp_longitude'],
            'projection_latitude': 0.,
            'projection_altitude': self.mda['projection_parameters']['h']}

        if self.fill_disk and not (dataset_id['name'] != 'HRV' and self.mda['is_full_disk']):
            padder = Padder(dataset_id,
                            self.image_boundaries.get_img_bounds(dataset_id, self.is_roi()),
                            self.mda['is_full_disk'])
            dataset = padder.pad_data(dataset)

        return dataset

    def _get_visir_channel(self, dataset_id):
        shape = (self.mda['number_of_lines'], self.mda['number_of_columns'])
        # Check if there is only 1 channel in the list as a change
        # is needed in the arrray assignment ie channl id is not present
        if len(self.mda['channel_list']) == 1:
            raw = self.dask_array['visir']['line_data']
        else:
            i = self.mda['channel_list'].index(dataset_id['name'])
            raw = self.dask_array['visir']['line_data'][:, i, :]
        data = dec10216(raw.flatten())
        data = data.reshape(shape)
        return data

    def _get_hrv_channel(self):
        shape = (self.mda['hrv_number_of_lines'], self.mda['hrv_number_of_columns'])
        shape_layer = (self.mda['number_of_lines'], self.mda['hrv_number_of_columns'])

        data_list = []
        for i in range(3):
            raw = self.dask_array['hrv']['line_data'][:, i, :]
            data = dec10216(raw.flatten())
            data = data.reshape(shape_layer)
            data_list.append(data)

        return np.stack(data_list, axis=1).reshape(shape)

    def calibrate(self, data, dataset_id):
        """Calibrate the data."""
        tic = datetime.now()

        data15hdr = self.header['15_DATA_HEADER']
        calibration = dataset_id['calibration']
        channel = dataset_id['name']

        # even though all the channels may not be present in the file,
        # the header does have calibration coefficients for all the channels
        # hence, this channel index needs to refer to full channel list
        i = list(CHANNEL_NAMES.values()).index(channel)

        if calibration == 'counts':
            return data

        if calibration in ['radiance', 'reflectance', 'brightness_temperature']:
            # determine the required calibration coefficients to use
            # for the Level 1.5 Header
            if (self.calib_mode.upper() != 'GSICS' and self.calib_mode.upper() != 'NOMINAL'):
                raise NotImplementedError(
                    'Unknown Calibration mode : Please check')

            # NB GSICS doesn't have calibration coeffs for VIS channels
            if (self.calib_mode.upper() != 'GSICS' or channel in VIS_CHANNELS):
                coeffs = data15hdr[
                    'RadiometricProcessing']['Level15ImageCalibration']
                gain = coeffs['CalSlope'][i]
                offset = coeffs['CalOffset'][i]
            else:
                coeffs = data15hdr[
                    'RadiometricProcessing']['MPEFCalFeedback']
                gain = coeffs['GSICSCalCoeff'][i]
                offset = coeffs['GSICSOffsetCount'][i]
                offset = offset * gain
            res = self._convert_to_radiance(data, gain, offset)

        if calibration == 'reflectance':
            solar_irradiance = CALIB[self.platform_id][channel]["F"]
            res = self._vis_calibrate(res, solar_irradiance)

        elif calibration == 'brightness_temperature':
            cal_type = data15hdr['ImageDescription'][
                'Level15ImageProduction']['PlannedChanProcessing'][i]
            res = self._ir_calibrate(res, channel, cal_type)

        logger.debug("Calibration time " + str(datetime.now() - tic))
        return res


class ImageBoundaries:
    """Collect image boundary information."""

    def __init__(self, header, trailer, mda):
        """Initialize the class."""
        self._header = header
        self._trailer = trailer
        self._mda = mda

    def get_img_bounds(self, dataset_id, is_roi):
        """Get image line and column boundaries.

        returns:
            Dictionary with the four keys 'south_bound', 'north_bound', 'east_bound' and 'west_bound',
            each containing a list of the respective line/column numbers of the image boundaries.

        Lists (rather than scalars) are returned since the HRV data in FES mode contain data from two windows/areas.
        """
        if dataset_id['name'] == 'HRV' and not is_roi:
            img_bounds = self._get_hrv_actual_img_bounds()
        else:
            img_bounds = self._get_selected_img_bounds(dataset_id)

        self._check_for_valid_bounds(img_bounds)

        return img_bounds

    def _get_hrv_actual_img_bounds(self):
        """Get HRV (if not ROI) image boundaries from the ActualL15CoverageHRV information stored in the trailer."""
        hrv_bounds = self._trailer['15TRAILER']['ImageProductionStats']['ActualL15CoverageHRV']

        img_bounds = {'south_bound': [], 'north_bound': [], 'east_bound': [], 'west_bound': []}
        for hrv_window in ['Lower', 'Upper']:
            img_bounds['south_bound'].append(hrv_bounds['%sSouthLineActual' % hrv_window])
            img_bounds['north_bound'].append(hrv_bounds['%sNorthLineActual' % hrv_window])
            img_bounds['east_bound'].append(hrv_bounds['%sEastColumnActual' % hrv_window])
            img_bounds['west_bound'].append(hrv_bounds['%sWestColumnActual' % hrv_window])

            # Data from the upper hrv window are only available in FES mode
            if not self._mda['is_full_disk']:
                break

        return img_bounds

    def _get_selected_img_bounds(self, dataset_id):
        """Get VISIR and HRV (if ROI) image boundaries from the SelectedRectangle information stored in the header."""
        sec15hd = self._header['15_SECONDARY_PRODUCT_HEADER']
        south_bound = int(sec15hd['SouthLineSelectedRectangle']['Value'])
        east_bound = int(sec15hd['EastColumnSelectedRectangle']['Value'])

        if dataset_id['name'] == 'HRV':
            nlines, ncolumns = self._get_hrv_img_shape()
            south_bound = self._convert_visir_bound_to_hrv(south_bound)
            east_bound = self._convert_visir_bound_to_hrv(east_bound)
        else:
            nlines, ncolumns = self._get_visir_img_shape()

        north_bound = south_bound + nlines - 1
        west_bound = east_bound + ncolumns - 1

        img_bounds = {'south_bound': [south_bound], 'north_bound': [north_bound],
                      'east_bound': [east_bound], 'west_bound': [west_bound]}

        return img_bounds

    def _get_hrv_img_shape(self):
        nlines = int(self._mda['hrv_number_of_lines'])
        ncolumns = int(self._mda['hrv_number_of_columns'])
        return nlines, ncolumns

    def _get_visir_img_shape(self):
        nlines = int(self._mda['number_of_lines'])
        ncolumns = int(self._mda['number_of_columns'])
        return nlines, ncolumns

    @staticmethod
    def _convert_visir_bound_to_hrv(bound):
        return 3 * bound - 2

    @staticmethod
    def _check_for_valid_bounds(img_bounds):
        len_img_bounds = [len(bound) for bound in img_bounds.values()]

        same_lengths = (len(set(len_img_bounds)) == 1)
        no_empty = (min(len_img_bounds) > 0)

        if not (same_lengths and no_empty):
            raise ValueError('Invalid image boundaries')


class Padder:
    """Padding of HRV, RSS and ROI data to full disk."""

    def __init__(self, dataset_id, img_bounds, is_full_disk):
        """Initialize the padder."""
        self._img_bounds = img_bounds
        self._is_full_disk = is_full_disk

        if dataset_id['name'] == 'HRV':
            self._final_shape = (HRV_NUM_LINES, HRV_NUM_COLUMNS)
        else:
            self._final_shape = (VISIR_NUM_LINES, VISIR_NUM_COLUMNS)

    def pad_data(self, dataset):
        """Pad data to full disk with empty pixels."""
        logger.debug('Padding data to full disk')

        data_list = []
        for south_bound, north_bound, east_bound, west_bound in zip(*self._img_bounds.values()):
            nlines = north_bound - south_bound + 1
            data = self._extract_data_to_pad(dataset, south_bound, north_bound)
            padded_data = pad_data_horizontally(data, (nlines, self._final_shape[1]), east_bound, west_bound)
            data_list.append(padded_data)

        padded_data = da.vstack(data_list)

        # If we're dealing with RSS or ROI data, we also need to pad vertically in order to form a full disk array
        if not self._is_full_disk:
            padded_data = pad_data_vertically(padded_data, self._final_shape, south_bound, north_bound)

        return xr.DataArray(padded_data, dims=('y', 'x'), attrs=dataset.attrs.copy())

    def _extract_data_to_pad(self, dataset, south_bound, north_bound):
        """Extract the data that shall be padded.

        In case of FES (HRV) data, 'dataset' contains data from twoseparate windows that
        are padded separately. Hence, we extract a subset of data.
        """
        if self._is_full_disk:
            data = dataset[south_bound - 1:north_bound, :].data
        else:
            data = dataset.data

        return data


def get_available_channels(header):
    """Get the available channels from the header information."""
    chlist_str = header['15_SECONDARY_PRODUCT_HEADER'][
        'SelectedBandIDs']['Value']
    retv = {}

    for idx, char in zip(range(12), chlist_str):
        retv[CHANNEL_NAMES[idx + 1]] = (char == 'X')

    return retv
