# -*- coding: utf-8 -*-
"""Test the MNE BIDS converter.

For each supported file format, implement a test.
"""
# Authors: Mainak Jas <mainak.jas@telecom-paristech.fr>
#          Teon L Brooks <teon.brooks@gmail.com>
#          Chris Holdgraf <choldgraf@berkeley.edu>
#          Stefan Appelhoff <stefan.appelhoff@mailbox.org>
#          Matt Sanderson <matt.sanderson@mq.edu.au>
#
# License: BSD (3-clause)
import os
import os.path as op
import pytest
from glob import glob
from datetime import datetime, timezone
import platform
import shutil as sh
import json
from distutils.version import LooseVersion
from pathlib import Path

import numpy as np
from numpy.testing import assert_array_equal, assert_array_almost_equal

# This is here to handle mne-python <0.20
import warnings
with warnings.catch_warnings():
    warnings.filterwarnings(action='ignore',
                            message="can't resolve package",
                            category=ImportWarning)
    import mne

from mne.datasets import testing
from mne.utils import (_TempDir, run_subprocess, check_version,
                       requires_nibabel, requires_version)
from mne.io import anonymize_info
from mne.io.constants import FIFF
from mne.io.kit.kit import get_kit_info

from mne_bids import (write_raw_bids, read_raw_bids, BIDSPath,
                      write_anat, make_dataset_description,
                      mark_bad_channels, write_meg_calibration,
                      write_meg_crosstalk)
from mne_bids.utils import (_stamp_to_dt, _get_anonymization_daysback,
                            get_anonymization_daysback)
from mne_bids.tsv_handler import _from_tsv, _to_tsv
from mne_bids.utils import _update_sidecar
from mne_bids.path import _find_matching_sidecar
from mne_bids.pick import coil_type
from mne_bids.config import REFERENCES

base_path = op.join(op.dirname(mne.__file__), 'io')
subject_id = '01'
subject_id2 = '02'
session_id = '01'
run = '01'
acq = '01'
run2 = '02'
task = 'testing'

_bids_path = BIDSPath(
    subject=subject_id, session=session_id, run=run, acquisition=acq,
    task=task)
_bids_path_minimal = BIDSPath(subject=subject_id, task=task)

warning_str = dict(
    channel_unit_changed='ignore:The unit for chann*.:RuntimeWarning:mne',
    meas_date_set_to_none="ignore:.*'meas_date' set to None:RuntimeWarning:"
                          "mne",
    nasion_not_found='ignore:.*nasion not found:RuntimeWarning:mne',
    annotations_omitted='ignore:Omitted .* annot.*:RuntimeWarning:mne',
)


def _wrap_read_raw(read_raw):
    def fn(fname, *args, **kwargs):
        raw = read_raw(fname, *args, **kwargs)
        raw.info['line_freq'] = 60
        return raw
    return fn


_read_raw_fif = _wrap_read_raw(mne.io.read_raw_fif)
_read_raw_ctf = _wrap_read_raw(mne.io.read_raw_ctf)
_read_raw_kit = _wrap_read_raw(mne.io.read_raw_kit)
_read_raw_bti = _wrap_read_raw(mne.io.read_raw_bti)
_read_raw_edf = _wrap_read_raw(mne.io.read_raw_edf)
_read_raw_bdf = _wrap_read_raw(mne.io.read_raw_bdf)
_read_raw_eeglab = _wrap_read_raw(mne.io.read_raw_eeglab)
_read_raw_brainvision = _wrap_read_raw(mne.io.read_raw_brainvision)
_read_raw_persyst = _wrap_read_raw(mne.io.read_raw_persyst)
_read_raw_nihon = _wrap_read_raw(mne.io.read_raw_nihon)

# parametrized directory, filename and reader for EEG/iEEG data formats
test_eegieeg_data = [
    ('EDF', 'test_reduced.edf', _read_raw_edf),
    ('Persyst', 'sub-pt1_ses-02_task-monitor_acq-ecog_run-01_clip2.lay', _read_raw_persyst),  # noqa
    ('NihonKohden', 'MB0400FU.EEG', _read_raw_nihon)
]


# WINDOWS issues:
# the bids-validator development version does not work properly on Windows as
# of 2019-06-25 --> https://github.com/bids-standard/bids-validator/issues/790
# As a workaround, we try to get the path to the executable from an environment
# variable VALIDATOR_EXECUTABLE ... if this is not possible we assume to be
# using the stable bids-validator and make a direct call of bids-validator
# also: for windows, shell = True is needed to call npm, bids-validator etc.
# see: https://stackoverflow.com/q/28891053/5201771
@pytest.fixture(scope="session")
def _bids_validate():
    """Fixture to run BIDS validator."""
    vadlidator_args = ['--config.error=41']
    exe = os.getenv('VALIDATOR_EXECUTABLE', 'bids-validator')

    if platform.system() == 'Windows':
        shell = True
    else:
        shell = False

    bids_validator_exe = [exe, *vadlidator_args]

    def _validate(bids_root):
        cmd = [*bids_validator_exe, bids_root]
        run_subprocess(cmd, shell=shell)

    return _validate


def _test_anonymize(raw, bids_path, events_fname=None, event_id=None):
    bids_root = _TempDir()
    bids_path = _bids_path.copy().update(root=bids_root)
    if raw.info['meas_date'] is not None:
        daysback, _ = get_anonymization_daysback(raw)
    else:
        # just pass back any arbitrary number if no measurement date
        daysback = 3300
    write_raw_bids(raw, bids_path, events_data=events_fname,
                   event_id=event_id, anonymize=dict(daysback=daysback),
                   overwrite=False)
    scans_tsv = BIDSPath(
        subject=subject_id, session=session_id,
        suffix='scans', extension='.tsv', root=bids_root)
    data = _from_tsv(scans_tsv)
    if data['acq_time'] is not None and data['acq_time'][0] != 'n/a':
        assert datetime.strptime(data['acq_time'][0],
                                 '%Y-%m-%dT%H:%M:%S').year < 1925

    return bids_root


def test_write_correct_inputs():
    """Test that inputs of write_raw_bids is correct."""
    data_path = testing.data_path()
    raw_fname = op.join(data_path, 'MEG', 'sample',
                        'sample_audvis_trunc_raw.fif')
    raw = _read_raw_fif(raw_fname)

    bids_path_str = 'sub-01_ses-01_meg.fif'
    with pytest.raises(RuntimeError, match='"bids_path" must be a '
                                           'BIDSPath object'):
        write_raw_bids(raw, bids_path_str)

    bids_path = _bids_path.copy().update(root=None)
    with pytest.raises(
            ValueError,
            match='The root of the "bids_path" must be set'):
        write_raw_bids(raw, bids_path)


def test_make_dataset_description():
    """Test making a dataset_description.json."""
    tmp_dir = _TempDir()
    with pytest.raises(ValueError, match='`dataset_type` must be either "raw" '
                                         'or "derivative."'):
        make_dataset_description(path=tmp_dir, name='tst', dataset_type='src')

    make_dataset_description(path=tmp_dir, name='tst')

    with open(op.join(tmp_dir, 'dataset_description.json'), 'r') as fid:
        dataset_description_json = json.load(fid)
        assert dataset_description_json["Authors"] == \
            ["Please cite MNE-BIDS in your publication before removing this "
             "(citations in README)"]

    make_dataset_description(
        path=tmp_dir, name='tst', authors='MNE B., MNE P.',
        funding='GSOC2019, GSOC2021',
        references_and_links='https://doi.org/10.21105/joss.01896',
        dataset_type='derivative', overwrite=False, verbose=True
    )

    with open(op.join(tmp_dir, 'dataset_description.json'), 'r') as fid:
        dataset_description_json = json.load(fid)
        assert dataset_description_json["Authors"] == \
            ["Please cite MNE-BIDS in your publication before removing this "
             "(citations in README)"]

    make_dataset_description(
        path=tmp_dir, name='tst2', authors='MNE B., MNE P.',
        funding='GSOC2019, GSOC2021',
        references_and_links='https://doi.org/10.21105/joss.01896',
        dataset_type='derivative', overwrite=True, verbose=True
    )

    with open(op.join(tmp_dir, 'dataset_description.json'), 'r') as fid:
        dataset_description_json = json.load(fid)
        assert dataset_description_json["Authors"] == ['MNE B.', 'MNE P.']

    with pytest.raises(ValueError, match='Previous BIDS version used'):
        version = make_dataset_description.__globals__['BIDS_VERSION']
        make_dataset_description.__globals__['BIDS_VERSION'] = 'old'
        make_dataset_description(path=tmp_dir, name='tst')
        # put version back so that it doesn't cause issues down the road
        make_dataset_description.__globals__['BIDS_VERSION'] = version


def test_stamp_to_dt():
    """Test conversions of meas_date to datetime objects."""
    meas_date = (1346981585, 835782)
    meas_datetime = _stamp_to_dt(meas_date)
    assert(meas_datetime == datetime(2012, 9, 7, 1, 33, 5, 835782,
                                     tzinfo=timezone.utc))
    meas_date = (1346981585,)
    meas_datetime = _stamp_to_dt(meas_date)
    assert(meas_datetime == datetime(2012, 9, 7, 1, 33, 5, 0,
                                     tzinfo=timezone.utc))


def test_get_anonymization_daysback():
    """Test daysback querying for anonymization."""
    data_path = testing.data_path()
    raw_fname = op.join(data_path, 'MEG', 'sample',
                        'sample_audvis_trunc_raw.fif')
    raw = _read_raw_fif(raw_fname)
    daysback_min, daysback_max = _get_anonymization_daysback(raw)
    # max_val off by 1 on Windows for some reason
    assert abs(daysback_min - 28461) < 2 and abs(daysback_max - 36880) < 2
    raw2 = raw.copy()
    raw2.info['meas_date'] = (np.int32(1158942080), np.int32(720100))
    raw3 = raw.copy()
    raw3.info['meas_date'] = (np.int32(914992080), np.int32(720100))
    daysback_min, daysback_max = get_anonymization_daysback([raw, raw2, raw3])
    assert abs(daysback_min - 29850) < 2 and abs(daysback_max - 35446) < 2
    raw4 = raw.copy()
    raw4.info['meas_date'] = (np.int32(4992080), np.int32(720100))
    raw5 = raw.copy()
    raw5.info['meas_date'] = None
    daysback_min2, daysback_max2 = get_anonymization_daysback([raw, raw2,
                                                               raw3, raw5])
    assert daysback_min2 == daysback_min and daysback_max2 == daysback_max
    with pytest.raises(ValueError, match='The dataset spans more time'):
        daysback_min, daysback_max = \
            get_anonymization_daysback([raw, raw2, raw4])


def test_create_fif(_bids_validate):
    """Test functionality for very short raw file created from data."""
    out_dir = _TempDir()
    bids_root = _TempDir()
    bids_path = _bids_path.copy().update(root=bids_root)
    sfreq, n_points = 1024., int(1e6)
    info = mne.create_info(['ch1', 'ch2', 'ch3', 'ch4', 'ch5'], sfreq,
                           ['seeg'] * 5)
    rng = np.random.RandomState(99)
    raw = mne.io.RawArray(rng.random((5, n_points)) * 1e-6, info)
    raw.info['line_freq'] = 60
    raw.save(op.join(out_dir, 'test-raw.fif'))
    raw = _read_raw_fif(op.join(out_dir, 'test-raw.fif'))
    write_raw_bids(raw, bids_path, verbose=False, overwrite=True)
    _bids_validate(bids_root)


@requires_version('pybv', '0.2.0')
@pytest.mark.filterwarnings(warning_str['annotations_omitted'])
@pytest.mark.filterwarnings(warning_str['channel_unit_changed'])
def test_fif(_bids_validate):
    """Test functionality of the write_raw_bids conversion for fif."""
    bids_root = _TempDir()
    bids_path = _bids_path.copy().update(root=bids_root, datatype='meg')
    data_path = testing.data_path()
    raw_fname = op.join(data_path, 'MEG', 'sample',
                        'sample_audvis_trunc_raw.fif')

    event_id = {'Auditory/Left': 1, 'Auditory/Right': 2, 'Visual/Left': 3,
                'Visual/Right': 4, 'Smiley': 5, 'Button': 32}
    events_fname = op.join(data_path, 'MEG', 'sample',
                           'sample_audvis_trunc_raw-eve.fif')

    raw = _read_raw_fif(raw_fname)
    # add data in as a montage for MEG
    ch_names = raw.ch_names
    elec_locs = np.random.random((len(ch_names), 3)).tolist()
    ch_pos = dict(zip(ch_names, elec_locs))
    meg_montage = mne.channels.make_dig_montage(ch_pos=ch_pos,
                                                coord_frame='head')
    raw.set_montage(meg_montage)

    write_raw_bids(raw, bids_path, events_data=events_fname,
                   event_id=event_id, overwrite=False)

    # Read the file back in to check that the data has come through cleanly.
    # Events and bad channel information was read through JSON sidecar files.
    with pytest.raises(TypeError, match="unexpected keyword argument 'foo'"):
        read_raw_bids(bids_path=bids_path, extra_params=dict(foo='bar'))

    raw2 = read_raw_bids(bids_path=bids_path,
                         extra_params=dict(allow_maxshield=True))
    assert set(raw.info['bads']) == set(raw2.info['bads'])
    events, _ = mne.events_from_annotations(raw2)
    events2 = mne.read_events(events_fname)
    events2 = events2[events2[:, 2] != 0]
    assert_array_equal(events2[:, 0], events[:, 0])

    # check if write_raw_bids works when there is no stim channel
    raw.set_channel_types({raw.ch_names[i]: 'misc'
                           for i in
                           mne.pick_types(raw.info, stim=True, meg=False)})
    bids_root = _TempDir()
    bids_path.update(root=bids_root)
    with pytest.warns(RuntimeWarning, match='No events found or provided.'):
        write_raw_bids(raw, bids_path, overwrite=False)

    _bids_validate(bids_root)

    # try with eeg data only (conversion to bv)
    bids_root = _TempDir()
    bids_path.update(root=bids_root)
    raw = _read_raw_fif(raw_fname)
    raw.load_data()
    raw2 = raw.pick_types(meg=False, eeg=True, stim=True, eog=True, ecg=True)
    raw2.save(op.join(bids_root, 'test-raw.fif'), overwrite=True)
    raw2 = mne.io.Raw(op.join(bids_root, 'test-raw.fif'), preload=False)
    events = mne.find_events(raw2)
    event_id = {'auditory/left': 1, 'auditory/right': 2, 'visual/left': 3,
                'visual/right': 4, 'smiley': 5, 'button': 32}
    epochs = mne.Epochs(raw2, events, event_id=event_id, tmin=-0.2, tmax=0.5,
                        preload=True)
    with pytest.warns(RuntimeWarning,
                      match='Converting data files to BrainVision format'):
        write_raw_bids(raw2, bids_path,
                       events_data=events_fname, event_id=event_id,
                       verbose=True, overwrite=False)
    bids_dir = op.join(bids_root, 'sub-%s' % subject_id,
                       'ses-%s' % session_id, 'eeg')
    sidecar_basename = bids_path.copy()
    for sidecar in ['channels.tsv', 'eeg.eeg', 'eeg.json', 'eeg.vhdr',
                    'eeg.vmrk', 'events.tsv']:
        suffix, extension = sidecar.split('.')
        sidecar_basename.update(suffix=suffix, extension=extension)
        assert op.isfile(op.join(bids_dir, sidecar_basename.basename))

    bids_path.update(root=bids_root, datatype='eeg')
    raw2 = read_raw_bids(bids_path=bids_path)
    os.remove(op.join(bids_root, 'test-raw.fif'))

    events2 = mne.find_events(raw2)
    epochs2 = mne.Epochs(raw2, events2, event_id=event_id, tmin=-0.2, tmax=0.5,
                         preload=True)
    assert_array_almost_equal(raw.get_data(), raw2.get_data())
    assert_array_almost_equal(epochs.get_data(), epochs2.get_data(), decimal=4)
    _bids_validate(bids_root)

    # write the same data but pretend it is empty room data:
    raw = _read_raw_fif(raw_fname)
    meas_date = raw.info['meas_date']
    if not isinstance(meas_date, datetime):
        meas_date = datetime.fromtimestamp(meas_date[0], tz=timezone.utc)
    er_date = meas_date.strftime('%Y%m%d')
    er_bids_path = BIDSPath(subject='emptyroom', session=er_date,
                            task='noise', root=bids_root)
    write_raw_bids(raw, er_bids_path, overwrite=False)
    assert op.exists(op.join(
        bids_root, 'sub-emptyroom', 'ses-{0}'.format(er_date), 'meg',
        'sub-emptyroom_ses-{0}_task-noise_meg.json'.format(er_date)))

    _bids_validate(bids_root)

    # test that an incorrect date raises an error.
    er_bids_basename_bad = BIDSPath(subject='emptyroom', session='19000101',
                                    task='noise', root=bids_root)
    with pytest.raises(ValueError, match='Date provided'):
        write_raw_bids(raw, er_bids_basename_bad, overwrite=False)

    # test that the acquisition time was written properly
    scans_tsv = BIDSPath(
        subject=subject_id, session=session_id,
        suffix='scans', extension='.tsv', root=bids_root)
    data = _from_tsv(scans_tsv)
    assert data['acq_time'][0] == meas_date.strftime('%Y-%m-%dT%H:%M:%S')

    # give the raw object some fake participant data (potentially overwriting)
    raw = _read_raw_fif(raw_fname)
    raw.info['subject_info'] = {'his_id': subject_id2,
                                'birthday': (1993, 1, 26), 'sex': 1, 'hand': 2}
    write_raw_bids(raw, bids_path, events_data=events_fname,
                   event_id=event_id, overwrite=True)
    # assert age of participant is correct
    participants_tsv = op.join(bids_root, 'participants.tsv')
    data = _from_tsv(participants_tsv)
    assert data['age'][data['participant_id'].index('sub-01')] == '9'

    # check to make sure participant data is overwritten, but keeps the fields
    data = _from_tsv(participants_tsv)
    participant_idx = data['participant_id'].index(f'sub-{subject_id}')
    # create a new test column in participants file tsv
    data['subject_test_col1'] = ['n/a'] * len(data['participant_id'])
    data['subject_test_col1'][participant_idx] = 'S'
    data['test_col2'] = ['n/a'] * len(data['participant_id'])
    orig_key_order = list(data.keys())
    _to_tsv(data, participants_tsv)
    # crate corresponding json entry
    participants_json_fpath = op.join(bids_root, 'participants.json')
    json_field = {
        'Description': 'trial-outcome',
        'Levels': {
            'S': 'success',
            'F': 'failure'
        }
    }
    _update_sidecar(participants_json_fpath, 'subject_test_col1', json_field)
    # bids root should still be valid because json reflects changes in tsv
    _bids_validate(bids_root)
    write_raw_bids(raw, bids_path, overwrite=True)
    data = _from_tsv(participants_tsv)
    with open(participants_json_fpath, 'r') as fin:
        participants_json = json.load(fin)
    assert 'subject_test_col1' in participants_json
    assert data['age'][data['participant_id'].index('sub-01')] == '9'
    assert data['subject_test_col1'][participant_idx] == 'S'
    # in addition assert the original ordering of the new overwritten file
    assert list(data.keys()) == orig_key_order

    # if overwrite is False, then nothing should change from the above
    with pytest.raises(FileExistsError, match='already exists'):
        raw.info['subject_info'] = None
        write_raw_bids(raw, bids_path, overwrite=False)
    data = _from_tsv(participants_tsv)
    with open(participants_json_fpath, 'r') as fin:
        participants_json = json.load(fin)
    assert 'subject_test_col1' in participants_json
    assert data['age'][data['participant_id'].index('sub-01')] == '9'
    assert data['subject_test_col1'][participant_idx] == 'S'
    # in addition assert the original ordering of the new overwritten file
    assert list(data.keys()) == orig_key_order

    # try and write preloaded data
    raw = _read_raw_fif(raw_fname, preload=True)
    with pytest.raises(ValueError, match='preloaded'):
        write_raw_bids(raw, bids_path, events_data=events_fname,
                       event_id=event_id, overwrite=False)

    # test anonymize
    raw = _read_raw_fif(raw_fname)
    raw.anonymize()

    data_path2 = _TempDir()
    raw_fname2 = op.join(data_path2, 'sample_audvis_raw.fif')
    raw.save(raw_fname2)

    # add some readme text
    readme = op.join(bids_root, 'README')
    with open(readme, 'w') as fid:
        fid.write('Welcome to my dataset\n')

    bids_path2 = bids_path.copy().update(subject=subject_id2)
    raw = _read_raw_fif(raw_fname2)
    bids_output_path = write_raw_bids(raw, bids_path2,
                                      events_data=events_fname,
                                      event_id=event_id, overwrite=False)

    # check that the overwrite parameters work correctly for the participant
    # data
    # change the gender but don't force overwrite.
    raw = _read_raw_fif(raw_fname)
    raw.info['subject_info'] = {'his_id': subject_id2,
                                'birthday': (1994, 1, 26), 'sex': 2, 'hand': 1}
    with pytest.raises(FileExistsError, match="already exists"):  # noqa: F821
        write_raw_bids(raw, bids_path2,
                       events_data=events_fname, event_id=event_id,
                       overwrite=False)

    # assert README has references in it
    with open(readme, 'r') as fid:
        text = fid.read()
        assert 'Welcome to my dataset\n' in text
        assert REFERENCES['mne-bids'] in text
        assert REFERENCES['meg'] in text
        assert REFERENCES['eeg'] not in text
        assert REFERENCES['ieeg'] not in text

    # now force the overwrite
    write_raw_bids(raw, bids_path2, events_data=events_fname,
                   event_id=event_id, overwrite=True)

    with open(readme, 'r') as fid:
        text = fid.read()
        assert 'Welcome to my dataset\n' in text
        assert REFERENCES['mne-bids'] in text
        assert REFERENCES['meg'] in text

    with pytest.raises(ValueError, match='raw_file must be'):
        write_raw_bids('blah', bids_path)

    del raw._filenames
    with pytest.raises(ValueError, match='raw.filenames is missing'):
        write_raw_bids(raw, bids_path2)

    _bids_validate(bids_root)

    assert op.exists(op.join(bids_root, 'participants.tsv'))

    # asserting that single fif files do not include the split key
    files = glob(op.join(bids_output_path, 'sub-' + subject_id2,
                         'ses-' + subject_id2, 'meg', '*.fif'))
    ii = 0
    for ii, FILE in enumerate(files):
        assert 'split' not in FILE
    assert ii < 1

    # check that split files have split key
    raw = _read_raw_fif(raw_fname)
    data_path3 = _TempDir()
    raw_fname3 = op.join(data_path3, 'sample_audvis_raw.fif')
    raw.save(raw_fname3, buffer_size_sec=1.0, split_size='10MB',
             split_naming='neuromag', overwrite=True)
    raw = _read_raw_fif(raw_fname3)
    subject_id3 = '03'
    bids_path3 = bids_path.copy().update(subject=subject_id3)
    bids_output_path = write_raw_bids(raw, bids_path3,
                                      overwrite=False)
    files = glob(op.join(bids_output_path, 'sub-' + subject_id3,
                         'ses-' + subject_id3, 'meg', '*.fif'))
    for FILE in files:
        assert 'split' in FILE

    # test unknown extension
    raw = _read_raw_fif(raw_fname)
    raw._filenames = (raw.filenames[0].replace('.fif', '.foo'),)
    with pytest.raises(ValueError, match='Unrecognized file format'):
        write_raw_bids(raw, bids_path)


@pytest.mark.skipif(LooseVersion(mne.__version__) < LooseVersion('0.20'),
                    reason="requires mne 0.20.dev0 or higher")
def test_fif_anonymize(_bids_validate):
    """Test write_raw_bids() with anonymization fif."""
    bids_root = _TempDir()
    bids_path = _bids_path.copy().update(root=bids_root)
    data_path = testing.data_path()
    raw_fname = op.join(data_path, 'MEG', 'sample',
                        'sample_audvis_trunc_raw.fif')

    event_id = {'Auditory/Left': 1, 'Auditory/Right': 2, 'Visual/Left': 3,
                'Visual/Right': 4, 'Smiley': 5, 'Button': 32}
    events_fname = op.join(data_path, 'MEG', 'sample',
                           'sample_audvis_trunc_raw-eve.fif')

    # test keyword mne-bids anonymize
    raw = _read_raw_fif(raw_fname)
    with pytest.raises(ValueError, match='`daysback` argument required'):
        write_raw_bids(raw, bids_path, events_data=events_fname,
                       event_id=event_id,
                       anonymize=dict(),
                       overwrite=True)

    bids_root = _TempDir()
    bids_path.update(root=bids_root)
    raw = _read_raw_fif(raw_fname)
    with pytest.warns(RuntimeWarning, match='daysback` is too small'):
        write_raw_bids(raw, bids_path, events_data=events_fname,
                       event_id=event_id,
                       anonymize=dict(daysback=400),
                       overwrite=False)

    bids_root = _TempDir()
    bids_path.update(root=bids_root)
    raw = _read_raw_fif(raw_fname)
    with pytest.raises(ValueError, match='`daysback` exceeds maximum value'):
        write_raw_bids(raw, bids_path, events_data=events_fname,
                       event_id=event_id,
                       anonymize=dict(daysback=40000),
                       overwrite=False)

    bids_root = _TempDir()
    bids_path.update(root=bids_root)
    raw = _read_raw_fif(raw_fname)
    write_raw_bids(raw, bids_path, events_data=events_fname,
                   event_id=event_id,
                   anonymize=dict(daysback=30000, keep_his=True),
                   overwrite=False)
    scans_tsv = BIDSPath(
        subject=subject_id, session=session_id,
        suffix='scans', extension='.tsv',
        root=bids_root)
    data = _from_tsv(scans_tsv)

    # anonymize using MNE manually
    anonymized_info = anonymize_info(info=raw.info, daysback=30000,
                                     keep_his=True)
    anon_date = anonymized_info['meas_date'].strftime("%Y-%m-%dT%H:%M:%S")
    assert data['acq_time'][0] == anon_date
    _bids_validate(bids_root)


def test_kit(_bids_validate):
    """Test functionality of the write_raw_bids conversion for KIT data."""
    bids_root = _TempDir()
    data_path = op.join(base_path, 'kit', 'tests', 'data')
    raw_fname = op.join(data_path, 'test.sqd')
    events_fname = op.join(data_path, 'test-eve.txt')
    hpi_fname = op.join(data_path, 'test_mrk.sqd')
    hpi_pre_fname = op.join(data_path, 'test_mrk_pre.sqd')
    hpi_post_fname = op.join(data_path, 'test_mrk_post.sqd')
    electrode_fname = op.join(data_path, 'test.elp')
    headshape_fname = op.join(data_path, 'test.hsp')
    event_id = dict(cond=1)

    kit_bids_path = _bids_path.copy().update(acquisition=None,
                                             root=bids_root,
                                             suffix='meg')

    raw = _read_raw_kit(
        raw_fname, mrk=hpi_fname, elp=electrode_fname,
        hsp=headshape_fname)
    write_raw_bids(raw, kit_bids_path,
                   events_data=events_fname,
                   event_id=event_id, overwrite=False)

    _bids_validate(bids_root)
    assert op.exists(op.join(bids_root, 'participants.tsv'))
    read_raw_bids(bids_path=kit_bids_path)

    # ensure the marker file is produced in the right place
    marker_fname = BIDSPath(
        subject=subject_id, session=session_id, task=task, run=run,
        suffix='markers', extension='.sqd',
        root=bids_root)
    assert op.exists(marker_fname)

    # test anonymize
    if check_version('mne', '0.20'):
        output_path = _test_anonymize(raw, kit_bids_path,
                                      events_fname, event_id)
        _bids_validate(output_path)
    else:
        with pytest.raises(ValueError, match='MNE is too old.'):
            output_path = _test_anonymize(raw, kit_bids_path,
                                          events_fname, event_id)

    # ensure the channels file has no STI 014 channel:
    channels_tsv = marker_fname.copy().update(datatype='meg',
                                              suffix='channels',
                                              extension='.tsv')
    data = _from_tsv(channels_tsv)
    assert 'STI 014' not in data['name']

    # ensure the marker file is produced in the right place
    assert op.exists(marker_fname)

    # test attempts at writing invalid event data
    event_data = np.loadtxt(events_fname)
    # make the data the wrong number of dimensions
    event_data_3d = np.atleast_3d(event_data)
    other_output_path = _TempDir()
    bids_path = _bids_path.copy().update(root=other_output_path)
    with pytest.raises(ValueError, match='two dimensions'):
        write_raw_bids(raw, bids_path, events_data=event_data_3d,
                       event_id=event_id, overwrite=True)
    # remove 3rd column
    event_data = event_data[:, :2]
    with pytest.raises(ValueError, match='second dimension'):
        write_raw_bids(raw, bids_path, events_data=event_data,
                       event_id=event_id, overwrite=True)
    # test correct naming of marker files
    raw = _read_raw_kit(
        raw_fname, mrk=[hpi_pre_fname, hpi_post_fname], elp=electrode_fname,
        hsp=headshape_fname)
    kit_bids_path.update(subject=subject_id2)
    write_raw_bids(raw, kit_bids_path, events_data=events_fname,
                   event_id=event_id, overwrite=False)

    _bids_validate(bids_root)
    # ensure the marker files are renamed correctly
    marker_fname.update(acquisition='pre', subject=subject_id2)
    info = get_kit_info(marker_fname, False)[0]
    assert info['meas_date'] == get_kit_info(hpi_pre_fname,
                                             False)[0]['meas_date']
    marker_fname.update(acquisition='post')
    info = get_kit_info(marker_fname, False)[0]
    assert info['meas_date'] == get_kit_info(hpi_post_fname,
                                             False)[0]['meas_date']

    # check that providing markers in the wrong order raises an error
    raw = _read_raw_kit(
        raw_fname, mrk=[hpi_post_fname, hpi_pre_fname], elp=electrode_fname,
        hsp=headshape_fname)
    with pytest.raises(ValueError, match='Markers'):
        write_raw_bids(raw, kit_bids_path.update(subject=subject_id2),
                       events_data=events_fname, event_id=event_id,
                       overwrite=True)


@pytest.mark.filterwarnings(warning_str['meas_date_set_to_none'])
def test_ctf(_bids_validate):
    """Test functionality of the write_raw_bids conversion for CTF data."""
    bids_root = _TempDir()
    data_path = op.join(testing.data_path(download=False), 'CTF')
    raw_fname = op.join(data_path, 'testdata_ctf.ds')
    bids_path = _bids_path.copy().update(root=bids_root, datatype='meg')

    raw = _read_raw_ctf(raw_fname)
    raw.info['line_freq'] = 60
    write_raw_bids(raw, bids_path)

    _bids_validate(bids_root)
    with pytest.warns(RuntimeWarning, match='Did not find any events'):
        raw = read_raw_bids(bids_path=bids_path,
                            extra_params=dict(clean_names=False))

    # test to check that running again with overwrite == False raises an error
    with pytest.raises(FileExistsError, match="already exists"):  # noqa: F821
        write_raw_bids(raw, bids_path)

    assert op.exists(op.join(bids_root, 'participants.tsv'))

    # test anonymize
    if check_version('mne', '0.20'):
        raw = _read_raw_ctf(raw_fname)
        with pytest.warns(RuntimeWarning,
                          match='Converting to FIF for anonymization'):
            output_path = _test_anonymize(raw, bids_path)
        _bids_validate(output_path)

        raw.set_meas_date(None)
        raw.anonymize()
        with pytest.raises(ValueError, match='All measurement dates are None'):
            get_anonymization_daysback(raw)


@pytest.mark.filterwarnings(warning_str['channel_unit_changed'])
def test_bti(_bids_validate):
    """Test functionality of the write_raw_bids conversion for BTi data."""
    bids_root = _TempDir()
    data_path = op.join(base_path, 'bti', 'tests', 'data')
    raw_fname = op.join(data_path, 'test_pdf_linux')
    config_fname = op.join(data_path, 'test_config_linux')
    headshape_fname = op.join(data_path, 'test_hs_linux')

    raw = _read_raw_bti(raw_fname, config_fname=config_fname,
                        head_shape_fname=headshape_fname)

    bids_path = _bids_path.copy().update(root=bids_root, datatype='meg')

    # write the BIDS dataset description, then write BIDS files
    make_dataset_description(bids_root, name="BTi data")
    write_raw_bids(raw, bids_path, verbose=True)

    assert op.exists(op.join(bids_root, 'participants.tsv'))
    _bids_validate(bids_root)

    raw = read_raw_bids(bids_path=bids_path)

    with pytest.raises(TypeError, match="unexpected keyword argument 'foo'"):
        read_raw_bids(bids_path=bids_path, extra_params=dict(foo='bar'))

    if check_version('mne', '0.20'):
        # test anonymize
        raw = _read_raw_bti(raw_fname, config_fname=config_fname,
                            head_shape_fname=headshape_fname)
        with pytest.warns(RuntimeWarning,
                          match='Converting to FIF for anonymization'):
            output_path = _test_anonymize(raw, bids_path)
        _bids_validate(output_path)


# XXX: vhdr test currently passes only on MNE master. Skip until next release.
@pytest.mark.skipif(LooseVersion(mne.__version__) < LooseVersion('0.21'),
                    reason='requires mne 0.21.dev0 or higher')
@pytest.mark.filterwarnings(warning_str['channel_unit_changed'])
def test_vhdr(_bids_validate):
    """Test write_raw_bids conversion for BrainVision data."""
    bids_root = _TempDir()
    data_path = op.join(base_path, 'brainvision', 'tests', 'data')
    raw_fname = op.join(data_path, 'test.vhdr')

    raw = _read_raw_brainvision(raw_fname)

    # inject a bad channel
    assert not raw.info['bads']
    injected_bad = ['FP1']
    raw.info['bads'] = injected_bad

    bids_path = _bids_path.copy().update(root=bids_root)
    bids_path_minimal = _bids_path_minimal.copy().update(root=bids_root,
                                                         datatype='eeg')

    # write with injected bad channels
    write_raw_bids(raw, bids_path_minimal, overwrite=False)
    _bids_validate(bids_root)

    # read and also get the bad channels
    raw = read_raw_bids(bids_path=bids_path_minimal)
    with pytest.raises(TypeError, match="unexpected keyword argument 'foo'"):
        read_raw_bids(bids_path=bids_path_minimal,
                      extra_params=dict(foo='bar'))

    # Check that injected bad channel shows up in raw after reading
    np.testing.assert_array_equal(np.asarray(raw.info['bads']),
                                  np.asarray(injected_bad))

    # Test that correct channel units are written ... and that bad channel
    # is in channels.tsv
    suffix, ext = 'channels', '.tsv'
    channels_tsv_name = bids_path_minimal.copy().update(
        suffix=suffix, extension=ext)

    data = _from_tsv(channels_tsv_name)
    assert data['units'][data['name'].index('FP1')] == 'µV'
    assert data['units'][data['name'].index('CP5')] == 'n/a'
    assert data['status'][data['name'].index(injected_bad[0])] == 'bad'
    status_description = data['status_description']
    assert status_description[data['name'].index(injected_bad[0])] == 'n/a'

    # check events.tsv is written
    events_tsv_fname = channels_tsv_name.update(suffix='events')
    assert op.exists(events_tsv_fname)

    # create another bids folder with the overwrite command and check
    # no files are in the folder
    data_path = BIDSPath(subject=subject_id, datatype='eeg',
                         root=bids_root).mkdir().directory
    assert len([f for f in os.listdir(data_path) if op.isfile(f)]) == 0

    # test anonymize and convert
    if check_version('mne', '0.20') and check_version('pybv', '0.2.0'):
        raw = _read_raw_brainvision(raw_fname)
        output_path = _test_anonymize(raw, bids_path)
        _bids_validate(output_path)

    # Also cover iEEG
    # We use the same data and pretend that eeg channels are ecog
    raw = _read_raw_brainvision(raw_fname)
    raw.set_channel_types({raw.ch_names[i]: 'ecog'
                           for i in mne.pick_types(raw.info, eeg=True)})
    bids_root = _TempDir()
    bids_path.update(root=bids_root)
    write_raw_bids(raw, bids_path, overwrite=False)
    _bids_validate(bids_root)

    # Test coords and impedance writing
    # first read the data and set a montage
    data_path = op.join(testing.data_path(), 'montage')
    fname_vhdr = op.join(data_path, 'bv_dig_test.vhdr')
    raw = _read_raw_brainvision(fname_vhdr, preload=False)
    raw.set_channel_types({'HEOG': 'eog', 'VEOG': 'eog', 'ECG': 'ecg'})
    fname_bvct = op.join(data_path, 'captrak_coords.bvct')
    montage = mne.channels.read_dig_captrak(fname_bvct)
    raw.set_montage(montage)

    # convert to BIDS and check impedances
    bids_root = _TempDir()
    bids_path.update(root=bids_root)
    write_raw_bids(raw, bids_path)
    electrodes_fpath = _find_matching_sidecar(
        bids_path.copy().update(root=bids_root),
        suffix='electrodes', extension='.tsv')
    tsv = _from_tsv(electrodes_fpath)
    assert len(tsv.get('impedance', {})) > 0
    assert tsv['impedance'][-3:] == ['n/a', 'n/a', 'n/a']
    assert tsv['impedance'][:3] == ['5.0', '2.0', '4.0']


@pytest.mark.parametrize('dir_name, fname, reader', test_eegieeg_data)
@pytest.mark.skipif(LooseVersion(mne.__version__) < LooseVersion('0.21'),
                    reason="requires mne 0.20.dev0 or higher")
@pytest.mark.filterwarnings(warning_str['nasion_not_found'])
def test_eegieeg(dir_name, fname, reader, _bids_validate):
    """Test write_raw_bids conversion for European Data Format data."""
    bids_root = _TempDir()
    data_path = op.join(testing.data_path(), dir_name)
    raw_fname = op.join(data_path, fname)

    raw = reader(raw_fname)

    raw.rename_channels({raw.info['ch_names'][0]: 'EOGtest'})
    raw.info['chs'][0]['coil_type'] = FIFF.FIFFV_COIL_EEG_BIPOLAR
    raw.rename_channels({raw.info['ch_names'][1]: 'EMG'})
    raw.set_channel_types({'EMG': 'emg'})
    bids_path = _bids_path.copy().update(root=bids_root, datatype='eeg')

    # test dataset description overwrites with the authors set
    make_dataset_description(bids_root, name="test",
                             authors=["test1", "test2"])
    write_raw_bids(raw, bids_path, overwrite=False)
    dataset_description_fpath = op.join(bids_root, "dataset_description.json")
    with open(dataset_description_fpath, 'r') as f:
        dataset_description_json = json.load(f)
        assert dataset_description_json["Authors"] == ["test1", "test2"]

    # write from fresh start w/ overwrite
    write_raw_bids(raw, bids_path, overwrite=True)
    # after overwrite, the dataset description if defaulted to MNE-BIDS
    with open(dataset_description_fpath, 'r') as f:
        dataset_description_json = json.load(f)
        assert dataset_description_json["Authors"] == \
            ["Please cite MNE-BIDS in your publication before removing this "
             "(citations in README)"]

    # Reading the file back should raise an error, because we renamed channels
    # in `raw` and used that information to write a channels.tsv. Yet, we
    # saved the unchanged `raw` in the BIDS folder, so channels in the TSV and
    # in raw clash
    # Note: only needed for data files that store channel names
    # alongside the data
    if dir_name == 'EDF':
        with pytest.raises(RuntimeError, match='Channels do not correspond'):
            read_raw_bids(bids_path=bids_path)

    with pytest.raises(TypeError, match="unexpected keyword argument 'foo'"):
        read_raw_bids(bids_path=bids_path, extra_params=dict(foo='bar'))

    bids_fname = bids_path.copy().update(run=run2)
    # add data in as a montage
    ch_names = raw.ch_names
    elec_locs = np.random.random((len(ch_names), 3))

    # test what happens if there is some nan entries
    elec_locs[-1, :] = [np.nan, np.nan, np.nan]
    ch_pos = dict(zip(ch_names, elec_locs.tolist()))
    eeg_montage = mne.channels.make_dig_montage(ch_pos=ch_pos,
                                                coord_frame='head')
    raw.set_montage(eeg_montage)
    # electrodes are not written w/o landmarks
    with pytest.warns(RuntimeWarning, match='Skipping EEG electrodes.tsv... '
                                            'Setting montage not possible'):
        write_raw_bids(raw, bids_fname, overwrite=True)

    electrodes_fpath = _find_matching_sidecar(bids_fname,
                                              suffix='electrodes',
                                              extension='.tsv',
                                              on_error='ignore')
    assert electrodes_fpath is None

    # with landmarks, eeg montage is written
    eeg_montage = mne.channels.make_dig_montage(ch_pos=ch_pos,
                                                coord_frame='head',
                                                nasion=[1, 0, 0],
                                                lpa=[0, 1, 0],
                                                rpa=[0, 0, 1])
    raw.set_montage(eeg_montage)
    write_raw_bids(raw, bids_fname, overwrite=True)
    electrodes_fpath = _find_matching_sidecar(bids_fname,
                                              suffix='electrodes',
                                              extension='.tsv')
    assert op.exists(electrodes_fpath)
    _bids_validate(bids_root)

    # ensure there is an EMG channel in the channels.tsv:
    channels_tsv = BIDSPath(
        subject=subject_id, session=session_id, task=task, run=run,
        suffix='channels', extension='.tsv', acquisition=acq,
        root=bids_root, datatype='eeg')
    data = _from_tsv(channels_tsv)
    assert 'ElectroMyoGram' in data['description']

    # check that the scans list contains two scans
    scans_tsv = BIDSPath(
        subject=subject_id, session=session_id,
        suffix='scans', extension='.tsv',
        root=bids_root)
    data = _from_tsv(scans_tsv)
    assert len(list(data.values())[0]) == 2

    # check that scans list is properly converted to brainvision
    if check_version('mne', '0.20') and check_version('pybv', '0.2.0'):
        daysback_min, daysback_max = _get_anonymization_daysback(raw)
        daysback = (daysback_min + daysback_max) // 2
        write_raw_bids(raw, bids_path,
                       anonymize=dict(daysback=daysback),
                       overwrite=True)
        data = _from_tsv(scans_tsv)
        bids_fname = bids_path.copy().update(suffix='eeg',
                                             extension='.vhdr')
        assert any([bids_fname.basename in fname
                    for fname in data['filename']])

    # Also cover iEEG
    # We use the same data and pretend that eeg channels are ecog
    ieeg_raw = raw.copy()

    # remove the old "EEG" montage, to test iEEG functionality
    ieeg_raw.set_montage(None)

    # convert channel types to ECoG and write BIDS
    eeg_picks = mne.pick_types(ieeg_raw.info, eeg=True)
    ieeg_raw.set_channel_types({raw.ch_names[i]: 'ecog'
                                for i in eeg_picks})
    bids_root = _TempDir()
    bids_path.update(root=bids_root)
    write_raw_bids(ieeg_raw, bids_path)
    _bids_validate(bids_root)

    # assert README has references in it
    readme = op.join(bids_root, 'README')
    with open(readme, 'r') as fid:
        text = fid.read()
        assert REFERENCES['ieeg'] in text
        assert REFERENCES['meg'] not in text
        assert REFERENCES['eeg'] not in text

    # test writing electrode coordinates (.tsv)
    # and coordinate system (.json)
    ch_names = ieeg_raw.ch_names
    elec_locs = np.random.random((len(ch_names), 3)).tolist()
    ch_pos = dict(zip(ch_names, elec_locs))
    ecog_montage = mne.channels.make_dig_montage(ch_pos=ch_pos,
                                                 coord_frame='mri')
    ieeg_raw.set_montage(ecog_montage)
    bids_root = _TempDir()
    bids_path.update(root=bids_root)
    write_raw_bids(ieeg_raw, bids_path)
    _bids_validate(bids_root)

    # XXX: Should be improved with additional coordinate system descriptions
    # iEEG montages written from mne-python end up as "Other"
    bids_fname.update(root=bids_root)
    electrodes_fname = _find_matching_sidecar(bids_fname,
                                              suffix='electrodes',
                                              extension='.tsv')
    coordsystem_fname = _find_matching_sidecar(bids_fname,
                                               suffix='coordsystem',
                                               extension='.json')
    assert 'space-mri' in electrodes_fname
    assert 'space-mri' in coordsystem_fname
    with open(coordsystem_fname, 'r') as fin:
        coordsystem_json = json.load(fin)
    assert coordsystem_json['iEEGCoordinateSystem'] == 'Other'

    # test anonymize and convert
    if check_version('mne', '0.20') and check_version('pybv', '0.2.0'):
        raw = reader(raw_fname)
        output_path = _test_anonymize(raw, bids_path)
        _bids_validate(output_path)


def test_bdf(_bids_validate):
    """Test write_raw_bids conversion for Biosemi data."""
    bids_root = _TempDir()
    data_path = op.join(base_path, 'edf', 'tests', 'data')
    raw_fname = op.join(data_path, 'test.bdf')

    bids_path = _bids_path.copy().update(root=bids_root)

    raw = _read_raw_bdf(raw_fname)
    raw.info['line_freq'] = 60
    write_raw_bids(raw, bids_path, overwrite=False)
    _bids_validate(bids_root)

    # assert README has references in it
    readme = op.join(bids_root, 'README')
    with open(readme, 'r') as fid:
        text = fid.read()
        assert REFERENCES['eeg'] in text
        assert REFERENCES['meg'] not in text
        assert REFERENCES['ieeg'] not in text

    # Test also the reading of channel types from channels.tsv
    # the first channel in the raw data is not MISC right now
    test_ch_idx = 0
    assert coil_type(raw.info, test_ch_idx) != 'misc'

    # we will change the channel type to MISC and overwrite the channels file
    bids_fname = bids_path.copy().update(suffix='eeg',
                                         extension='.bdf')
    channels_fname = _find_matching_sidecar(bids_fname,
                                            suffix='channels',
                                            extension='.tsv')
    channels_dict = _from_tsv(channels_fname)
    channels_dict['type'][test_ch_idx] = 'MISC'
    _to_tsv(channels_dict, channels_fname)

    # Now read the raw data back from BIDS, with the tampered TSV, to show
    # that the channels.tsv truly influences how read_raw_bids sets ch_types
    # in the raw data object
    bids_path.update(datatype='eeg')
    raw = read_raw_bids(bids_path=bids_path)
    assert coil_type(raw.info, test_ch_idx) == 'misc'
    with pytest.raises(TypeError, match="unexpected keyword argument 'foo'"):
        read_raw_bids(bids_path=bids_path, extra_params=dict(foo='bar'))

    # Test cropped assertion error
    raw = _read_raw_bdf(raw_fname)
    raw.crop(0, raw.times[-2])
    with pytest.raises(AssertionError, match='cropped'):
        write_raw_bids(raw, bids_path)

    # test anonymize and convert
    if check_version('mne', '0.20') and check_version('pybv', '0.2.0'):
        raw = _read_raw_bdf(raw_fname)
        output_path = _test_anonymize(raw, bids_path)
        _bids_validate(output_path)


@pytest.mark.filterwarnings(warning_str['meas_date_set_to_none'])
def test_set(_bids_validate):
    """Test write_raw_bids conversion for EEGLAB data."""
    # standalone .set file with associated .fdt
    bids_root = _TempDir()
    data_path = op.join(testing.data_path(), 'EEGLAB')
    raw_fname = op.join(data_path, 'test_raw.set')
    raw = _read_raw_eeglab(raw_fname)
    bids_path = _bids_path.copy().update(root=bids_root, datatype='eeg')

    # embedded - test mne-version assertion
    tmp_version = mne.__version__
    mne.__version__ = '0.16'
    with pytest.raises(ValueError, match='Your version of MNE is too old.'):
        write_raw_bids(raw, bids_path)
    mne.__version__ = tmp_version

    # proceed with the actual test for EEGLAB data
    write_raw_bids(raw, bids_path, overwrite=False)
    read_raw_bids(bids_path=bids_path)

    with pytest.raises(TypeError, match="unexpected keyword argument 'foo'"):
        read_raw_bids(bids_path=bids_path, extra_params=dict(foo='bar'))

    with pytest.raises(FileExistsError, match="already exists"):  # noqa: F821
        write_raw_bids(raw, bids_path, overwrite=False)
    _bids_validate(bids_root)

    # check events.tsv is written
    # XXX: only from 0.18 onwards because events_from_annotations
    # is broken for earlier versions
    events_tsv_fname = op.join(bids_root, 'sub-' + subject_id,
                               'ses-' + session_id, 'eeg',
                               bids_path.basename + '_events.tsv')
    if check_version('mne', '0.18'):
        assert op.exists(events_tsv_fname)

    # Also cover iEEG
    # We use the same data and pretend that eeg channels are ecog
    raw.set_channel_types({raw.ch_names[i]: 'ecog'
                           for i in mne.pick_types(raw.info, eeg=True)})
    bids_root = _TempDir()
    bids_path.update(root=bids_root)
    write_raw_bids(raw, bids_path)
    _bids_validate(bids_root)

    # test anonymize and convert
    if check_version('mne', '0.20') and check_version('pybv', '0.2.0'):
        output_path = _test_anonymize(raw, bids_path)
        _bids_validate(output_path)


@requires_nibabel()
def test_write_anat(_bids_validate):
    """Test writing anatomical data."""
    # Get the MNE testing sample data
    import nibabel as nib
    bids_root = _TempDir()
    data_path = testing.data_path()
    raw_fname = op.join(data_path, 'MEG', 'sample',
                        'sample_audvis_trunc_raw.fif')

    event_id = {'Auditory/Left': 1, 'Auditory/Right': 2, 'Visual/Left': 3,
                'Visual/Right': 4, 'Smiley': 5, 'Button': 32}
    events_fname = op.join(data_path, 'MEG', 'sample',
                           'sample_audvis_trunc_raw-eve.fif')

    raw = _read_raw_fif(raw_fname)
    bids_path = _bids_path.copy().update(root=bids_root)
    write_raw_bids(raw, bids_path, events_data=events_fname,
                   event_id=event_id, overwrite=False)

    # Write some MRI data and supply a `trans`
    trans_fname = raw_fname.replace('_raw.fif', '-trans.fif')
    trans = mne.read_trans(trans_fname)

    # Get the T1 weighted MRI data file
    # Needs to be converted to Nifti because we only have mgh in our test base
    t1w_mgh = op.join(data_path, 'subjects', 'sample', 'mri', 'T1.mgz')

    bids_path = BIDSPath(subject=subject_id, session=session_id,
                         acquisition=acq, root=bids_root)
    bids_path = write_anat(t1w_mgh, bids_path=bids_path,
                           raw=raw, trans=trans, deface=True, verbose=True,
                           overwrite=True)
    anat_dir = bids_path.directory
    _bids_validate(bids_root)

    # Validate that files are as expected
    t1w_json_path = op.join(anat_dir, 'sub-01_ses-01_acq-01_T1w.json')
    assert op.exists(t1w_json_path)
    assert op.exists(op.join(anat_dir, 'sub-01_ses-01_acq-01_T1w.nii.gz'))
    with open(t1w_json_path, 'r') as f:
        t1w_json = json.load(f)

    # We only should have AnatomicalLandmarkCoordinates as key
    np.testing.assert_array_equal(list(t1w_json.keys()),
                                  ['AnatomicalLandmarkCoordinates'])
    # And within AnatomicalLandmarkCoordinates only LPA, NAS, RPA in that order
    anat_dict = t1w_json['AnatomicalLandmarkCoordinates']
    point_list = ['LPA', 'NAS', 'RPA']
    np.testing.assert_array_equal(list(anat_dict.keys()),
                                  point_list)
    sidecar_basename = BIDSPath(subject='01', session='01',
                                acquisition='01', root=bids_root,
                                suffix='T1w', extension='.nii.gz')
    # test the actual values of the voxels (no floating points)
    for i, point in enumerate([(66, 51, 46), (41, 32, 74), (17, 53, 47)]):
        coords = anat_dict[point_list[i]]
        np.testing.assert_array_equal(np.asarray(coords, dtype=int),
                                      point)

        # BONUS: test also that we can find the matching sidecar
        side_fname = _find_matching_sidecar(sidecar_basename,
                                            suffix='T1w',
                                            extension='.json')
        assert op.split(side_fname)[-1] == 'sub-01_ses-01_acq-01_T1w.json'

    # Now try some anat writing that will fail
    # We already have some MRI data there
    with pytest.raises(IOError, match='`overwrite` is set to False'):
        write_anat(t1w_mgh, bids_path=bids_path,
                   raw=raw, trans=trans, verbose=True, deface=False,
                   overwrite=False)

    # pass some invalid type as T1 MRI
    with pytest.raises(ValueError, match='must be a path to a T1 weighted'):
        write_anat(9999999999999, bids_path=bids_path, raw=raw,
                   trans=trans, verbose=True, deface=False, overwrite=True)

    # Return without writing sidecar
    sh.rmtree(anat_dir)
    write_anat(t1w_mgh, bids_path=bids_path)
    # Assert that we truly cannot find a sidecar
    with pytest.raises(RuntimeError, match='Did not find any'):
        _find_matching_sidecar(sidecar_basename,
                               suffix='T1w', extension='.json')

    # trans has a wrong type
    wrong_type = 1
    if check_version('mne', min_version='0.21'):
        match = f'trans must be an instance of .*, got {type(wrong_type)} '
        ex = TypeError
    else:
        match = f'transform type {type(wrong_type)} not known, must be'
        ex = ValueError

    with pytest.raises(ex, match=match):
        write_anat(t1w_mgh, bids_path=bids_path, raw=raw,
                   trans=wrong_type, verbose=True, deface=False,
                   overwrite=True)

    # trans is a str, but file does not exist
    wrong_fname = 'not_a_trans'
    match = 'trans file "{}" not found'.format(wrong_fname)
    with pytest.raises(IOError, match=match):
        write_anat(t1w_mgh, bids_path=bids_path, raw=raw,
                   trans=wrong_fname, verbose=True, overwrite=True)

    # However, reading trans if it is a string pointing to trans is fine
    write_anat(t1w_mgh, bids_path=bids_path, raw=raw,
               trans=trans_fname, verbose=True, deface=False,
               overwrite=True)

    # Writing without a session does NOT yield "ses-None" anywhere
    bids_path.update(session=None, acquisition=None)
    bids_path = write_anat(t1w_mgh, bids_path=bids_path)
    anat_dir2 = bids_path.directory
    assert 'ses-None' not in anat_dir2.as_posix()
    assert op.exists(op.join(anat_dir2, 'sub-01_T1w.nii.gz'))

    # specify trans but not raw
    with pytest.raises(ValueError, match='must be specified if `trans`'):
        bids_path.update(session=session_id)
        write_anat(t1w_mgh, bids_path=bids_path, raw=None,
                   trans=trans, verbose=True, deface=False, overwrite=True)

    # test deface
    bids_path = write_anat(t1w_mgh, bids_path=bids_path,
                           raw=raw, trans=trans_fname,
                           verbose=True, deface=True, overwrite=True)
    anat_dir = bids_path.directory
    t1w = nib.load(op.join(anat_dir, 'sub-01_ses-01_T1w.nii.gz'))
    vox_sum = t1w.get_fdata().sum()

    bids_path = write_anat(t1w_mgh, bids_path=bids_path,
                           raw=raw, trans=trans_fname,
                           verbose=True, deface=dict(inset=25.),
                           overwrite=True)
    anat_dir2 = bids_path.directory
    t1w2 = nib.load(op.join(anat_dir2, 'sub-01_ses-01_T1w.nii.gz'))
    vox_sum2 = t1w2.get_fdata().sum()

    assert vox_sum > vox_sum2

    bids_path = write_anat(t1w_mgh, bids_path=bids_path,
                           raw=raw, trans=trans_fname,
                           verbose=True, deface=dict(theta=25),
                           overwrite=True)
    anat_dir3 = bids_path.directory
    t1w3 = nib.load(op.join(anat_dir3, 'sub-01_ses-01_T1w.nii.gz'))
    vox_sum3 = t1w3.get_fdata().sum()

    assert vox_sum > vox_sum3

    with pytest.raises(ValueError,
                       match='The raw object, trans and raw or the landmarks'):
        write_anat(t1w_mgh, bids_path=bids_path, raw=raw,
                   trans=None, verbose=True, deface=True,
                   overwrite=True)

    with pytest.raises(ValueError, match='inset must be numeric'):
        write_anat(t1w_mgh, bids_path=bids_path, raw=raw,
                   trans=trans, verbose=True, deface=dict(inset='small'),
                   overwrite=True)

    with pytest.raises(ValueError, match='inset should be positive'):
        write_anat(t1w_mgh, bids_path=bids_path, raw=raw,
                   trans=trans, verbose=True, deface=dict(inset=-2.),
                   overwrite=True)

    with pytest.raises(ValueError, match='theta must be numeric'):
        write_anat(t1w_mgh, bids_path=bids_path, raw=raw,
                   trans=trans, verbose=True, deface=dict(theta='big'),
                   overwrite=True)

    with pytest.raises(ValueError,
                       match='theta should be between 0 and 90 degrees'):
        write_anat(t1w_mgh, bids_path=bids_path, raw=raw,
                   trans=trans, verbose=True, deface=dict(theta=100),
                   overwrite=True)

    # Write some MRI data and supply `landmarks`
    mri_voxel_landmarks = mne.channels.make_dig_montage(
        lpa=[66.08580, 51.33362, 46.52982],
        nasion=[41.87363, 32.24694, 74.55314],
        rpa=[17.23812, 53.08294, 47.01789],
        coord_frame='mri_voxel')

    mri_landmarks = mne.channels.make_dig_montage(
        lpa=[-0.07629625, -0.00062556, -0.00776012],
        nasion=[0.00267222, 0.09362256, 0.03224791],
        rpa=[0.07635873, -0.00258065, -0.01212903],
        coord_frame='mri')

    meg_landmarks = mne.channels.make_dig_montage(
        lpa=[-7.13766068e-02, 0.00000000e+00, 5.12227416e-09],
        nasion=[3.72529030e-09, 1.02605611e-01, 4.19095159e-09],
        rpa=[7.52676800e-02, 0.00000000e+00, 5.58793545e-09],
        coord_frame='head')

    # test mri voxel landmarks
    bids_path.update(acquisition=acq)
    bids_path = write_anat(t1w_mgh, bids_path=bids_path,
                           deface=True, landmarks=mri_voxel_landmarks,
                           verbose=True, overwrite=True)
    anat_dir = bids_path.directory
    _bids_validate(bids_root)

    t1w1 = nib.load(op.join(anat_dir, 'sub-01_ses-01_acq-01_T1w.nii.gz'))
    vox1 = t1w1.get_fdata()

    # test mri landmarks
    bids_path = write_anat(t1w_mgh, bids_path=bids_path, deface=True,
                           landmarks=mri_landmarks, verbose=True,
                           overwrite=True)
    anat_dir = bids_path.directory
    _bids_validate(bids_root)

    t1w2 = nib.load(op.join(anat_dir, 'sub-01_ses-01_acq-01_T1w.nii.gz'))
    vox2 = t1w2.get_fdata()

    # because of significant rounding errors the voxels are fairly different
    # but the deface works in all three cases and was checked
    assert abs(vox1 - vox2).sum() / abs(vox1).sum() < 0.2

    # crash for raw also
    with pytest.raises(ValueError, match='Please use either `landmarks`'):
        write_anat(t1w_mgh, bids_path=bids_path, raw=raw, trans=trans,
                   deface=True, landmarks=mri_landmarks, verbose=True,
                   overwrite=True)

    # crash for trans also
    with pytest.raises(ValueError, match='`trans` was provided'):
        write_anat(t1w_mgh, bids_path=bids_path, trans=trans, deface=True,
                   landmarks=mri_landmarks, verbose=True, overwrite=True)

    # test meg landmarks
    tmp_dir = _TempDir()
    meg_landmarks.save(op.join(tmp_dir, 'meg_landmarks.fif'))
    bids_path = write_anat(t1w_mgh, bids_path=bids_path, deface=True,
                           trans=trans,
                           landmarks=op.join(tmp_dir, 'meg_landmarks.fif'),
                           verbose=True, overwrite=True)
    anat_dir = bids_path.directory
    _bids_validate(bids_root)

    t1w3 = nib.load(op.join(anat_dir, 'sub-01_ses-01_acq-01_T1w.nii.gz'))
    vox3 = t1w3.get_fdata()

    assert abs(vox1 - vox3).sum() / abs(vox1).sum() < 0.2

    # test raise error on meg_landmarks with no trans
    with pytest.raises(ValueError, match='Head space landmarks provided'):
        write_anat(t1w_mgh, bids_path=bids_path, deface=True,
                   landmarks=meg_landmarks, verbose=True, overwrite=True)

    # test unsupported (any coord_frame other than head and mri) coord_frame
    fail_landmarks = meg_landmarks.copy()
    fail_landmarks.dig[0]['coord_frame'] = 3
    fail_landmarks.dig[1]['coord_frame'] = 3
    fail_landmarks.dig[2]['coord_frame'] = 3

    with pytest.raises(ValueError, match='Coordinate frame not recognized'):
        write_anat(t1w_mgh, bids_path=bids_path, deface=True,
                   landmarks=fail_landmarks, verbose=True, overwrite=True)


def test_write_raw_pathlike():
    data_path = testing.data_path()
    raw_fname = op.join(data_path, 'MEG', 'sample',
                        'sample_audvis_trunc_raw.fif')
    event_id = {'Auditory/Left': 1, 'Auditory/Right': 2, 'Visual/Left': 3,
                'Visual/Right': 4, 'Smiley': 5, 'Button': 32}
    raw = _read_raw_fif(raw_fname)

    bids_root = Path(_TempDir())
    events_fname = \
        Path(data_path) / 'MEG' / 'sample' / 'sample_audvis_trunc_raw-eve.fif'
    bids_path = _bids_path.copy().update(root=bids_root)
    bids_path_ = write_raw_bids(raw=raw, bids_path=bids_path,
                                events_data=events_fname,
                                event_id=event_id, overwrite=False)

    # write_raw_bids() should return a string.
    assert isinstance(bids_path_, BIDSPath)
    assert bids_path_.root == bids_root


def test_write_raw_no_dig():
    data_path = testing.data_path()
    raw_fname = op.join(data_path, 'MEG', 'sample',
                        'sample_audvis_trunc_raw.fif')
    raw = _read_raw_fif(raw_fname)
    bids_root = Path(_TempDir())
    bids_path = _bids_path.copy().update(root=bids_root)
    bids_path_ = write_raw_bids(raw=raw, bids_path=bids_path,
                                overwrite=True)
    assert bids_path_.root == bids_root
    raw.info['dig'] = None
    raw.save(str(bids_root / 'tmp_raw.fif'))
    raw = _read_raw_fif(bids_root / 'tmp_raw.fif')
    bids_path_ = write_raw_bids(raw=raw, bids_path=bids_path,
                                overwrite=True)
    assert bids_path_.root == bids_root
    assert bids_path_.suffix == 'meg'
    assert bids_path_.extension == '.fif'


@requires_nibabel()
def test_write_anat_pathlike():
    """Test writing anatomical data with pathlib.Paths."""
    data_path = testing.data_path()
    raw_fname = op.join(data_path, 'MEG', 'sample',
                        'sample_audvis_trunc_raw.fif')
    trans_fname = raw_fname.replace('_raw.fif', '-trans.fif')
    raw = _read_raw_fif(raw_fname)
    trans = mne.read_trans(trans_fname)

    bids_root = Path(_TempDir())
    t1w_mgh_fname = Path(data_path) / 'subjects' / 'sample' / 'mri' / 'T1.mgz'
    bids_path = BIDSPath(subject=subject_id, session=session_id,
                         acquisition=acq, root=bids_root)
    bids_path = write_anat(t1w=t1w_mgh_fname, bids_path=bids_path, raw=raw,
                           trans=trans, deface=True, verbose=True,
                           overwrite=True)

    # write_anat() should return a BIDSPath.
    assert isinstance(bids_path, BIDSPath)


def test_write_does_not_alter_events_inplace():
    """Test that writing does not modify the passed events array."""
    data_path = testing.data_path()
    raw_fname = op.join(data_path, 'MEG', 'sample',
                        'sample_audvis_trunc_raw.fif')
    events_fname = op.join(data_path, 'MEG', 'sample',
                           'sample_audvis_trunc_raw-eve.fif')

    raw = _read_raw_fif(raw_fname)
    events = mne.read_events(events_fname)
    events_orig = events.copy()

    bids_root = _TempDir()
    bids_path = _bids_path.copy().update(root=bids_root)
    write_raw_bids(raw=raw, bids_path=bids_path,
                   events_data=events, overwrite=True)

    assert np.array_equal(events, events_orig)


def _ensure_list(x):
    """Return a list representation of the input."""
    if isinstance(x, str):
        return [x]
    elif x is None:
        return []
    else:
        return list(x)


@pytest.mark.parametrize(
    'ch_names, descriptions, drop_status_col, drop_description_col, '
    'existing_ch_names, existing_descriptions, datatype, overwrite',
    [
        # Only mark channels, do not set descriptions.
        (['MEG 0112', 'MEG 0131', 'EEG 053'], None, False, False, [], [], None,
         False),
        ('MEG 0112', None, False, False, [], [], None, False),
        ('nonsense', None, False, False, [], [], None, False),
        # Now also set descriptions.
        (['MEG 0112', 'MEG 0131'], ['Really bad!', 'Even worse.'], False,
         False, [], [], None, False),
        ('MEG 0112', 'Really bad!', False, False, [], [], None, False),
        (['MEG 0112', 'MEG 0131'], ['Really bad!'], False, False, [], [], None,
         False),  # Should raise.
        # `datatype='meg`
        (['MEG 0112'], ['Really bad!'], False, False, [], [], 'meg', False),
        # Enure we create missing columns.
        ('MEG 0112', 'Really bad!', True, True, [], [], None, False),
        # Ensure existing entries are left untouched if `overwrite=False`
        (['EEG 053'], ['Just testing'], False, False, ['MEG 0112', 'MEG 0131'],
         ['Really bad!', 'Even worse.'], None, False),
        # Ensure existing entries are discarded if `overwrite=True`.
        (['EEG 053'], ['Just testing'], False, False, ['MEG 0112', 'MEG 0131'],
         ['Really bad!', 'Even worse.'], None, True)
    ])
@pytest.mark.filterwarnings(warning_str['channel_unit_changed'])
def test_mark_bad_channels(_bids_validate,
                           ch_names, descriptions,
                           drop_status_col, drop_description_col,
                           existing_ch_names, existing_descriptions,
                           datatype, overwrite):
    """Test marking channels of an existing BIDS dataset as "bad"."""

    # Setup: Create a fresh BIDS dataset.
    bids_root = _TempDir()
    bids_path = _bids_path.copy().update(root=bids_root, datatype='meg',
                                         suffix='meg')
    data_path = testing.data_path()
    raw_fname = op.join(data_path, 'MEG', 'sample',
                        'sample_audvis_trunc_raw.fif')
    event_id = {'Auditory/Left': 1, 'Auditory/Right': 2, 'Visual/Left': 3,
                'Visual/Right': 4, 'Smiley': 5, 'Button': 32}
    events_fname = op.join(data_path, 'MEG', 'sample',
                           'sample_audvis_trunc_raw-eve.fif')
    raw = _read_raw_fif(raw_fname, verbose=False)
    raw.info['bads'] = []
    write_raw_bids(raw, bids_path=bids_path, events_data=events_fname,
                   event_id=event_id, verbose=False)

    channels_fname = _find_matching_sidecar(bids_path, suffix='channels',
                                            extension='.tsv')

    if drop_status_col:
        # Remove `status` column from the sidecare TSV file.
        tsv_data = _from_tsv(channels_fname)
        del tsv_data['status']
        _to_tsv(tsv_data, channels_fname)

    if drop_description_col:
        # Remove `status_description` column from the sidecare TSV file.
        tsv_data = _from_tsv(channels_fname)
        del tsv_data['status_description']
        _to_tsv(tsv_data, channels_fname)

    # Test that we raise if number of channels doesn't match number of
    # descriptions.
    if (descriptions is not None and
            len(_ensure_list(ch_names)) != len(_ensure_list(descriptions))):
        with pytest.raises(ValueError, match='must match'):
            mark_bad_channels(ch_names=ch_names, descriptions=descriptions,
                              bids_path=bids_path, overwrite=overwrite)
        return

    # Test that we raise if we encounter an unknown channel name.
    if any([ch_name not in raw.ch_names
            for ch_name in _ensure_list(ch_names)]):
        with pytest.raises(ValueError, match='not found in dataset'):
            mark_bad_channels(ch_names=ch_names, descriptions=descriptions,
                              bids_path=bids_path, overwrite=overwrite)
        return

    if not overwrite:
        # Mark `existing_ch_names` as bad in raw and sidecar TSV before we
        # begin our actual tests, which should then add additional channels
        # to the list of bads, retaining the ones we're specifying here.
        mark_bad_channels(ch_names=existing_ch_names,
                          descriptions=existing_descriptions,
                          bids_path=bids_path, overwrite=True)
        _bids_validate(bids_root)
        raw = read_raw_bids(bids_path=bids_path, verbose=False)
        # Order is not preserved
        assert set(existing_ch_names) == set(raw.info['bads'])
        del raw

    mark_bad_channels(ch_names=ch_names, descriptions=descriptions,
                      bids_path=bids_path, overwrite=overwrite)
    _bids_validate(bids_root)
    raw = read_raw_bids(bids_path=bids_path, verbose=False)

    if drop_status_col or overwrite:
        # Existing column values should have been discarded, so only the new
        # ones should be present.
        expected_bads = _ensure_list(ch_names)
    else:
        expected_bads = (_ensure_list(ch_names) +
                         _ensure_list(existing_ch_names))

    if drop_description_col or overwrite:
        # Existing column values should have been discarded, so only the new
        # ones should be present.
        expected_descriptions = _ensure_list(descriptions)
    else:
        expected_descriptions = (_ensure_list(descriptions) +
                                 _ensure_list(existing_descriptions))

    # Order is not preserved
    assert len(expected_bads) == len(raw.info['bads'])
    assert set(expected_bads) == set(raw.info['bads'])

    # Descriptions are not mapped to Raw, so let's check the TSV contents
    # directly.
    tsv_data = _from_tsv(channels_fname)
    assert 'status' in tsv_data
    assert 'status_description' in tsv_data
    for description in expected_descriptions:
        assert description in tsv_data['status_description']


@pytest.mark.filterwarnings(warning_str['channel_unit_changed'])
def test_mark_bad_channels_files():
    """Test validity of bad channel writing."""
    # BV
    bids_root = _TempDir()
    data_path = op.join(base_path, 'brainvision', 'tests', 'data')
    raw_fname = op.join(data_path, 'test.vhdr')

    raw = _read_raw_brainvision(raw_fname)

    # inject a bad channel
    assert not raw.info['bads']
    injected_bad = ['FP1']
    raw.info['bads'] = injected_bad

    bids_path = _bids_path.copy().update(root=bids_root)

    # write with injected bad channels
    write_raw_bids(raw, bids_path, overwrite=True)

    # TODO: allow write_brain_vision to write different units
    # mark bad channels that get stored as uV in write_brain_vision
    bads = ['CP5', 'CP6', 'HL', 'HR', 'Vb', 'ReRef']
    mark_bad_channels(bads, bids_path=bids_path, overwrite=False)
    raw.info['bads'].extend(bads)

    # the raw data should match without the bads
    raw_2 = read_raw_bids(bids_path)

    # if you drop the bads they should match
    raw.drop_channels(raw.info['bads'])
    raw_2.drop_channels(raw_2.info['bads'])
    assert_array_almost_equal(raw.get_data(), raw_2.get_data())

    # EDF won't work
    dir_name = 'EDF'
    fname = 'test_reduced.edf'
    bids_root = _TempDir()
    bids_path = _bids_path.copy().update(root=bids_root)
    data_path = op.join(testing.data_path(), dir_name)
    raw_fname = op.join(data_path, fname)
    raw = _read_raw_edf(raw_fname)

    # write edf
    write_raw_bids(raw, bids_path, overwrite=True)

    # try to mark bad channels
    with pytest.raises(RuntimeError, match='Can only mark bad'):
        mark_bad_channels(raw.ch_names[0], bids_path=bids_path)


@pytest.mark.skipif('BIDS_VALIDATOR_VERSION' in os.environ and
                    LooseVersion(os.environ['BIDS_VALIDATOR_VERSION']) <
                    LooseVersion('1.5.5'),
                    reason=('requires bids-validator 1.5.5 or newer'))
def test_write_meg_calibration(_bids_validate):
    """Test writing of the Elekta/Neuromag fine-calibration file."""
    bids_root = _TempDir()
    bids_path = _bids_path.copy().update(root=bids_root)

    data_path = Path(testing.data_path())

    raw_fname = op.join(data_path, 'MEG', 'sample',
                        'sample_audvis_trunc_raw.fif')
    raw = _read_raw_fif(raw_fname, verbose=False)
    write_raw_bids(raw, bids_path=bids_path, verbose=False)

    fine_cal_fname = data_path / 'SSS' / 'sss_cal_mgh.dat'

    # Test passing a filename.
    write_meg_calibration(calibration=fine_cal_fname,
                          bids_path=bids_path)
    _bids_validate(bids_root)

    # Test passing a dict.
    calibration = mne.preprocessing.read_fine_calibration(fine_cal_fname)
    write_meg_calibration(calibration=calibration,
                          bids_path=bids_path)
    _bids_validate(bids_root)

    # Test passing in incompatible dict.
    calibration = mne.preprocessing.read_fine_calibration(fine_cal_fname)
    del calibration['locs']
    with pytest.raises(ValueError, match='not .* proper fine-calibration'):
        write_meg_calibration(calibration=calibration,
                              bids_path=bids_path)

    # subject not set.
    bids_path = bids_path.copy().update(root=bids_root, subject=None)
    with pytest.raises(ValueError, match='must have root and subject set'):
        write_meg_calibration(fine_cal_fname, bids_path)

    # root not set.
    bids_path = bids_path.copy().update(subject='01', root=None)
    with pytest.raises(ValueError, match='must have root and subject set'):
        write_meg_calibration(fine_cal_fname, bids_path)

    # datatype is not 'meg.
    bids_path = bids_path.copy().update(subject='01', root=bids_root,
                                        datatype='eeg')
    with pytest.raises(ValueError, match='Can only write .* for MEG'):
        write_meg_calibration(fine_cal_fname, bids_path)


@pytest.mark.skipif('BIDS_VALIDATOR_VERSION' in os.environ and
                    LooseVersion(os.environ['BIDS_VALIDATOR_VERSION']) <
                    LooseVersion('1.5.5'),
                    reason=('requires bids-validator 1.5.5 or newer'))
def test_write_meg_crosstalk(_bids_validate):
    """Test writing of the Elekta/Neuromag fine-calibration file."""
    bids_root = _TempDir()
    bids_path = _bids_path.copy().update(root=bids_root)
    data_path = Path(testing.data_path())

    raw_fname = op.join(data_path, 'MEG', 'sample',
                        'sample_audvis_trunc_raw.fif')
    raw = _read_raw_fif(raw_fname, verbose=False)
    write_raw_bids(raw, bids_path=bids_path, verbose=False)

    crosstalk_fname = data_path / 'SSS' / 'ct_sparse.fif'

    write_meg_crosstalk(fname=crosstalk_fname, bids_path=bids_path)
    _bids_validate(bids_root)

    # subject not set.
    bids_path = bids_path.copy().update(root=bids_root, subject=None)
    with pytest.raises(ValueError, match='must have root and subject set'):
        write_meg_crosstalk(crosstalk_fname, bids_path)

    # root not set.
    bids_path = bids_path.copy().update(subject='01', root=None)
    with pytest.raises(ValueError, match='must have root and subject set'):
        write_meg_crosstalk(crosstalk_fname, bids_path)

    # datatype is not 'meg'.
    bids_path = bids_path.copy().update(subject='01', root=bids_root,
                                        datatype='eeg')
    with pytest.raises(ValueError, match='Can only write .* for MEG'):
        write_meg_crosstalk(crosstalk_fname, bids_path)
