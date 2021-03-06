"""Read/write BIDS compatible electrode/coords structures from MNE."""
# Authors: Adam Li <adam2392@gmail.com>
#          Stefan Appelhoff <stefan.appelhoff@mailbox.org>
#
# License: BSD (3-clause)
import json
from collections import OrderedDict

import mne
import numpy as np
from mne.io.constants import FIFF
from mne.utils import _check_ch_locs, logger, warn

from mne_bids.config import (BIDS_IEEG_COORDINATE_FRAMES,
                             BIDS_MEG_COORDINATE_FRAMES,
                             BIDS_EEG_COORDINATE_FRAMES,
                             BIDS_COORDINATE_UNITS,
                             MNE_TO_BIDS_FRAMES, BIDS_TO_MNE_FRAMES,
                             MNE_FRAME_TO_STR, COORD_FRAME_DESCRIPTIONS)
from mne_bids.tsv_handler import _from_tsv
from mne_bids.utils import (_extract_landmarks, _scale_coord_to_meters,
                            _write_json, _write_tsv)
from mne_bids import BIDSPath
from mne_bids.path import get_entities_from_fname


def _handle_electrodes_reading(electrodes_fname, coord_frame,
                               coord_unit, raw, verbose):
    """Read associated electrodes.tsv and populate raw.

    Handle xyz coordinates and coordinate frame of each channel.
    Assumes units of coordinates are in 'm'.
    """
    logger.info('Reading electrode '
                'coords from {}.'.format(electrodes_fname))
    electrodes_dict = _from_tsv(electrodes_fname)
    # First, make sure that ordering of names in channels.tsv matches the
    # ordering of names in the raw data. The "name" column is mandatory in BIDS
    ch_names_raw = list(raw.ch_names)
    ch_names_tsv = electrodes_dict['name']

    if ch_names_raw != ch_names_tsv:
        msg = ('Channels do not correspond between raw data and the '
               'channels.tsv file. For MNE-BIDS, the channel names in the '
               'tsv MUST be equal and in the same order as the channels in '
               'the raw data.\n\n'
               '{} channels in tsv file: "{}"\n\n --> {}\n\n'
               '{} channels in raw file: "{}"\n\n --> {}\n\n'
               .format(len(ch_names_tsv), electrodes_fname, ch_names_tsv,
                       len(ch_names_raw), raw.filenames, ch_names_raw)
               )

        # XXX: this could be due to MNE inserting a 'STI 014' channel as the
        # last channel: In that case, we can work. --> Can be removed soon,
        # because MNE will stop the synthesis of stim channels in the near
        # future
        if not (ch_names_raw[-1] == 'STI 014' and
                ch_names_raw[:-1] == ch_names_tsv):
            raise RuntimeError(msg)

    if verbose:
        summary_str = [(ch, coord) for idx, (ch, coord)
                       in enumerate(electrodes_dict.items())
                       if idx < 5]
        print("The read in electrodes file is: \n", summary_str)

    def _float_or_nan(val):
        if val == "n/a":
            return np.nan
        else:
            return float(val)

    # convert coordinates to float and create list of tuples
    electrodes_dict['x'] = [_float_or_nan(x) for x in electrodes_dict['x']]
    electrodes_dict['y'] = [_float_or_nan(x) for x in electrodes_dict['y']]
    electrodes_dict['z'] = [_float_or_nan(x) for x in electrodes_dict['z']]
    ch_names_raw = [x for i, x in enumerate(ch_names_raw)
                    if electrodes_dict['x'][i] != "n/a"]
    ch_locs = np.c_[electrodes_dict['x'],
                    electrodes_dict['y'],
                    electrodes_dict['z']]

    # determine if there are problematic channels
    nan_chs = []
    for ch_name, ch_coord in zip(ch_names_raw, ch_locs):
        if any(np.isnan(ch_coord)) and ch_name not in raw.info['bads']:
            nan_chs.append(ch_name)
    if len(nan_chs) > 0:
        warn("There are channels without locations "
             "(n/a) that are not marked as bad: {}".format(nan_chs))

    # convert coordinates to meters
    ch_locs = _scale_coord_to_meters(ch_locs, coord_unit)

    # create mne.DigMontage
    ch_pos = dict(zip(ch_names_raw, ch_locs))
    montage = mne.channels.make_dig_montage(ch_pos=ch_pos,
                                            coord_frame=coord_frame)
    raw.set_montage(montage)
    return raw


def _handle_coordsystem_reading(coordsystem_fpath, datatype, verbose=True):
    """Read associated coordsystem.json.

    Handle reading the coordinate frame and coordinate unit
    of each electrode.
    """
    # open coordinate system sidecar json
    with open(coordsystem_fpath, 'r') as fin:
        coordsystem_json = json.load(fin)

    if datatype == 'meg':
        coord_frame = coordsystem_json['MEGCoordinateSystem'].lower()
        coord_unit = coordsystem_json['MEGCoordinateUnits']
        coord_frame_desc = coordsystem_json.get('MEGCoordinateDescription',
                                                None)
    elif datatype == 'eeg':
        coord_frame = coordsystem_json['EEGCoordinateSystem'].lower()
        coord_unit = coordsystem_json['EEGCoordinateUnits']
        coord_frame_desc = coordsystem_json.get('EEGCoordinateDescription',
                                                None)
    elif datatype == 'ieeg':
        coord_frame = coordsystem_json['iEEGCoordinateSystem'].lower()
        coord_unit = coordsystem_json['iEEGCoordinateUnits']
        coord_frame_desc = coordsystem_json.get('iEEGCoordinateDescription',
                                                None)

    if verbose:
        print(f"Reading in coordinate system frame {coord_frame}: "
              f"{coord_frame_desc}.")

    return coord_frame, coord_unit


def _get_impedances(raw, names):
    """Get the impedance values in kOhm from raw.impedances."""
    if not hasattr(raw, 'impedances'):  # pragma: no cover
        return ['n/a'] * len(names)
    no_info = {'imp': 'n/a', 'imp_unit': 'kOhm'}
    impedance_dicts = [raw.impedances.get(name, no_info) for name in names]
    # If we encounter a unit not defined in `scalings`, return NaN
    scalings = {'kOhm': 1, 'Ohm': 0.001}
    impedances = [
        imp_dict['imp'] * scalings.get(imp_dict['imp_unit'], np.nan)
        for imp_dict in impedance_dicts
    ]
    return impedances


def _electrodes_tsv(raw, fname, datatype, overwrite=False, verbose=True):
    """Create an electrodes.tsv file and save it.

    Parameters
    ----------
    raw : instance of Raw
        The data as MNE-Python Raw object.
    fname : str
        Filename to save the electrodes.tsv to.
    datatype : str
        Type of the data recording. Can be ``meg``, ``eeg``,
        or ``ieeg``.
    overwrite : bool
        Defaults to False.
        Whether to overwrite the existing data in the file.
        If there is already data for the given `fname` and overwrite is False,
        an error will be raised.
    verbose : bool
        Set verbose output to true or false.
    """
    # create list of channel coordinates and names
    x, y, z, names = list(), list(), list(), list()
    for ch in raw.info['chs']:
        if _check_ch_locs([ch]):
            x.append(ch['loc'][0])
            y.append(ch['loc'][1])
            z.append(ch['loc'][2])
        else:
            x.append('n/a')
            y.append('n/a')
            z.append('n/a')
        names.append(ch['ch_name'])

    # create OrderedDict to write to tsv file
    if datatype == "ieeg":
        # XXX: size should be included in the future
        sizes = ['n/a'] * len(names)
        data = OrderedDict([('name', names),
                            ('x', x),
                            ('y', y),
                            ('z', z),
                            ('size', sizes),
                            ])
    elif datatype == 'eeg':
        data = OrderedDict([('name', names),
                            ('x', x),
                            ('y', y),
                            ('z', z),
                            ])
    else:  # pragma: no cover
        raise RuntimeError("datatype {} not supported.".format(datatype))

    # Add impedance values if available, currently only BrainVision:
    # https://github.com/mne-tools/mne-python/pull/7974
    if hasattr(raw, 'impedances'):
        data['impedance'] = _get_impedances(raw, names)

    _write_tsv(fname, data, overwrite=overwrite, verbose=verbose)


def _coordsystem_json(raw, unit, orient, coordsystem_name, fname,
                      datatype, overwrite=False, verbose=True):
    """Create a coordsystem.json file and save it.

    Parameters
    ----------
    raw : instance of Raw
        The data as MNE-Python Raw object.
    unit : str
        Units to be used in the coordsystem specification,
        as in BIDS_COORDINATE_UNITS.
    orient : str
        Used to define the coordinate system for the head coils.
    coordsystem_name : str
        Name of the coordinate system for the sensor positions.
    fname : str
        Filename to save the coordsystem.json to.
    datatype : str
        Type of the data recording. Can be ``meg``, ``eeg``,
        or ``ieeg``.
    overwrite : bool
        Whether to overwrite the existing file.
        Defaults to False.
    verbose : bool
        Set verbose output to true or false.

    """
    dig = raw.info['dig']
    if dig is None:
        dig = []
    coords = _extract_landmarks(dig)
    hpi = {d['ident']: d for d in dig if d['kind'] == FIFF.FIFFV_POINT_HPI}
    if hpi:
        for ident in hpi.keys():
            coords['coil%d' % ident] = hpi[ident]['r'].tolist()

    coord_frame = set([dig[ii]['coord_frame'] for ii in range(len(dig))])
    if len(coord_frame) > 1:  # noqa E501
        raise ValueError('All HPI, electrodes, and fiducials must be in the '
                         'same coordinate frame. Found: "{}"'
                         .format(coord_frame))

    # get the coordinate frame description
    coordsystem_desc = COORD_FRAME_DESCRIPTIONS.get(coordsystem_name, "n/a")
    if coordsystem_name == 'Other' and verbose:
        print('Using the `Other` keyword for the CoordinateSystem field. '
              'Please specify the CoordinateSystemDescription field manually.')

    # create the coordinate json data structure based on 'datatype'
    if datatype == 'meg':
        hpi = {d['ident']: d for d in dig if d['kind'] == FIFF.FIFFV_POINT_HPI}
        if hpi:
            for ident in hpi.keys():
                coords['coil%d' % ident] = hpi[ident]['r'].tolist()

        fid_json = {
            'MEGCoordinateSystem': coordsystem_name,
            'MEGCoordinateUnits': unit,  # XXX validate this
            'MEGCoordinateSystemDescription': coordsystem_desc,
            'HeadCoilCoordinates': coords,
            'HeadCoilCoordinateSystem': orient,
            'HeadCoilCoordinateUnits': unit  # XXX validate this
        }
    elif datatype == 'eeg':
        fid_json = {
            'EEGCoordinateSystem': coordsystem_name,
            'EEGCoordinateUnits': unit,
            'EEGCoordinateSystemDescription': coordsystem_desc,
            'AnatomicalLandmarkCoordinates': coords,
            'AnatomicalLandmarkCoordinateSystem': coordsystem_name,
            'AnatomicalLandmarkCoordinateUnits': unit,
        }
    elif datatype == "ieeg":
        fid_json = {
            'iEEGCoordinateSystem': coordsystem_name,  # (Other, Pixels, ACPC)
            'iEEGCoordinateSystemDescription': coordsystem_desc,
            'iEEGCoordinateUnits': unit,  # m (MNE), mm, cm , or pixels
        }

    _write_json(fname, fid_json, overwrite, verbose)


def _write_dig_bids(electrodes_path, coordsystem_path, root,
                    raw, datatype, overwrite=False, verbose=True):
    """Write BIDS formatted DigMontage from Raw instance.

    Handles coordinatesystem.json and electrodes.tsv writing
    from DigMontage.

    Parameters
    ----------
    electrodes_path : str
        Filename to save the electrodes.tsv to.
    coordsystem_path : str
        Filename to save the coordsystem.json to.
    root : str | pathlib.Path
        Path to the data directory
    raw : instance of Raw
        The data as MNE-Python Raw object.
    datatype : str
        Type of the data recording. Can be ``meg``, ``eeg``,
        or ``ieeg``.
    overwrite : bool
        Whether to overwrite the existing file.
        Defaults to False.
    verbose : bool
        Set verbose output to true or false.
    """
    # write electrodes data for iEEG and EEG
    unit = "m"  # defaults to meters

    params = get_entities_from_fname(electrodes_path)
    subject_id = params['subject']
    session_id = params['session']
    acquisition = params['acquisition']

    # get coordinate frame from digMontage
    digpoint = raw.info['dig'][0]
    if any(digpoint['coord_frame'] != _digpoint['coord_frame']
           for _digpoint in raw.info['dig']):
        warn("Not all digpoints have the same coordinate frame. "
             "Skipping electrodes.tsv writing...")
        return

    # get the accepted mne-python coordinate frames
    coord_frame_int = int(digpoint['coord_frame'])
    mne_coord_frame = MNE_FRAME_TO_STR.get(coord_frame_int, None)
    coord_frame = MNE_TO_BIDS_FRAMES.get(mne_coord_frame, None)

    if verbose:
        print("Writing electrodes file to... ", electrodes_path)
        print("Writing coordsytem file to... ", coordsystem_path)

    if datatype == "ieeg":
        if coord_frame is not None:
            # XXX: To improve when mne-python allows coord_frame='unknown'
            if coord_frame not in BIDS_IEEG_COORDINATE_FRAMES:
                coordsystem_path = BIDSPath(
                    subject=subject_id, session=session_id,
                    acquisition=acquisition, space=coord_frame,
                    suffix='coordsystem', extension='.json',
                    root=root)
                electrodes_path = BIDSPath(
                    subject=subject_id, session=session_id,
                    acquisition=acquisition, space=coord_frame,
                    suffix='electrodes', extension='.tsv',
                    root=root)
                coord_frame = 'Other'

            # Now write the data to the elec coords and the coordsystem
            _electrodes_tsv(raw, electrodes_path,
                            datatype, overwrite, verbose)
            _coordsystem_json(raw, unit, 'n/a',
                              coord_frame, coordsystem_path, datatype,
                              overwrite, verbose)
        else:
            # default coordinate frame to mri if not available
            warn("Coordinate frame of iEEG coords missing/unknown "
                 "for {}. Skipping reading "
                 "in of montage...".format(electrodes_path))
    elif datatype == 'eeg':
        # We only write EEG electrodes.tsv and coordsystem.json
        # if we have LPA, RPA, and NAS available to rescale to a known
        # coordinate system frame
        coords = _extract_landmarks(raw.info['dig'])
        landmarks = set(['RPA', 'NAS', 'LPA']) == set(list(coords.keys()))

        # XXX: to be improved to allow rescaling if landmarks are present
        # mne-python automatically converts unknown coord frame to head
        if coord_frame_int == FIFF.FIFFV_COORD_HEAD and landmarks:
            # Now write the data
            _electrodes_tsv(raw, electrodes_path, datatype,
                            overwrite, verbose)
            _coordsystem_json(raw, 'm', 'RAS', 'CapTrak',
                              coordsystem_path, datatype,
                              overwrite, verbose)
        else:
            warn("Skipping EEG electrodes.tsv... "
                 "Setting montage not possible if anatomical "
                 "landmarks (NAS, LPA, RPA) are missing, "
                 "and coord_frame is not 'head'.")


def _read_dig_bids(electrodes_fpath, coordsystem_fpath,
                   raw, datatype, verbose):
    """Read MNE-Python formatted DigMontage from BIDS files.

    Handles coordinatesystem.json and electrodes.tsv reading
    to DigMontage.

    Parameters
    ----------
    electrodes_fpath : str
        Filepath of the electrodes.tsv to read.
    coordsystem_fpath : str
        Filepath of the coordsystem.json to read.
    raw : instance of Raw
        The data as MNE-Python Raw object.
    datatype : str
        Type of the data recording. Can be ``meg``, ``eeg``,
        or ``ieeg``.
    verbose : bool
        Set verbose output to true or false.

    Returns
    -------
    raw : instance of Raw
        The data as MNE-Python Raw object.
    """
    # get the space entity
    params = get_entities_from_fname(electrodes_fpath)
    space = params['space']
    if space is None:
        space = ''
    space = space.lower()

    # read in coordinate information
    coord_frame, coord_unit = _handle_coordsystem_reading(coordsystem_fpath,
                                                          datatype, verbose)

    if datatype == 'meg':
        if coord_frame not in BIDS_MEG_COORDINATE_FRAMES:
            warn("MEG Coordinate frame is not accepted "
                 "BIDS keyword. The allowed keywords are: "
                 "{}".format(BIDS_MEG_COORDINATE_FRAMES))
            coord_frame = None
        elif coord_frame == 'other':
            warn("Coordinate frame of MEG data can't be determined "
                 "when 'other'. The currently accepted keywords are: "
                 "{}".format(BIDS_MEG_COORDINATE_FRAMES))
            coord_frame = None
        else:
            coord_frame = BIDS_TO_MNE_FRAMES.get(coord_frame, None)
    elif datatype == 'ieeg':
        if coord_frame not in BIDS_IEEG_COORDINATE_FRAMES:
            warn("iEEG Coordinate frame is not accepted "
                 "BIDS keyword. The allowed keywords are: "
                 "{}".format(BIDS_IEEG_COORDINATE_FRAMES))
            coord_frame = None
        elif coord_frame == 'pixels':
            warn("Coordinate frame of iEEG data in pixels does not "
                 "get read in by mne-python. Skipping reading of "
                 "electrodes.tsv ...")
            coord_frame = None
        elif coord_frame == 'acpc':
            coord_frame = BIDS_TO_MNE_FRAMES.get(coord_frame, None)
        elif coord_frame == 'other':
            # XXX: We allow 'other' coordinate frames, but must be mne-python
            if space not in BIDS_TO_MNE_FRAMES:
                # default coordinate frames to available ones in mne-python
                # noqa: see https://bids-specification.readthedocs.io/en/stable/99-appendices/08-coordinate-systems.html
                warn("Defaulting coordinate frame to unknown "
                     "from coordinate system input {}".format(coord_frame))
            coord_frame = BIDS_TO_MNE_FRAMES.get(space, None)
    elif datatype == 'eeg':
        # only accept captrak
        if coord_frame not in BIDS_EEG_COORDINATE_FRAMES:
            warn("EEG Coordinate frame is not accepted "
                 "BIDS keyword. The allowed keywords are: "
                 "{}".format(BIDS_IEEG_COORDINATE_FRAMES))
            coord_frame = None
        else:
            coord_frame = BIDS_TO_MNE_FRAMES.get(coord_frame, None)

    # check coordinate units
    if coord_unit not in BIDS_COORDINATE_UNITS:
        warn("Coordinate unit is not an accepted BIDS unit for {}. "
             "Please specify to be one of {}. Skipping electrodes.tsv "
             "reading..."
             .format(electrodes_fpath, BIDS_COORDINATE_UNITS))
        coord_frame = None

    # only set montage if coordinate frame was properly parsed
    if coord_frame is not None:
        # read in electrode coordinates and attach to raw
        raw = _handle_electrodes_reading(electrodes_fpath, coord_frame,
                                         coord_unit, raw, verbose)

    return raw
