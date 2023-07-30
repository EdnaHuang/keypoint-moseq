import jax.numpy as jnp
import jax
import numpy as np
import warnings
import h5py
import joblib
import tqdm
import yaml
import os
import pandas as pd
from textwrap import fill
import sleap_io
from pynwb import NWBHDF5IO
from ndx_pose import PoseEstimation

from keypoint_moseq.util import (
    list_files_with_exts, 
    check_nan_proportions,
)


def _build_yaml(sections, comments):
    text_blocks = []
    for title,data in sections:
        centered_title = f' {title} '.center(50, '=')
        text_blocks.append(f"\n\n{'#'}{centered_title}{'#'}")
        for key,value in data.items():
            text = yaml.dump({key:value}).strip('\n')
            if key in comments: text = f"\n{'#'} {comments[key]}\n{text}"
            text_blocks.append(text)
    return '\n'.join(text_blocks)
        

def _get_path(project_dir, name, path, filename, pathname_for_error_msg='path'):
    if path is None: 
        assert project_dir is not None and name is not None, fill(
            f'`name` and `project_dir` are required if no `{pathname_for_error_msg}` is given.')
        path = os.path.join(project_dir, name, filename)
    return path


def generate_config(project_dir, **kwargs):
    """
    Generate a `config.yml` file with project settings. Default 
    settings will be used unless overriden by a keyword argument.
    
    Parameters
    ----------
    project_dir: str 
        A file `config.yml` will be generated in this directory.
    
    kwargs
        Custom project settings.  
    """
    
    def _update_dict(new, original):
        return {k:new[k] if k in new else v for k,v in original.items()} 
    
    hypperams = _update_dict(kwargs, {
        'error_estimator': {'slope':-.5, 'intercept':.25},
        'obs_hypparams': {'sigmasq_0':0.1, 'sigmasq_C':.1, 'nu_sigma':1e5, 'nu_s':5},
        'ar_hypparams': {'latent_dim': 10, 'nlags': 3, 'S_0_scale': 0.01, 'K_0_scale': 10.0},
        'trans_hypparams': {'num_states': 100, 'gamma': 1e3, 'alpha': 5.7, 'kappa': 1e6},
        'cen_hypparams': {'sigmasq_loc': 0.5}})
    
    hypperams = {k : _update_dict(kwargs, v) for k,v in hypperams.items()}

    anatomy = _update_dict(kwargs, {
        'bodyparts': ['BODYPART1','BODYPART2','BODYPART3'],
        'use_bodyparts': ['BODYPART1','BODYPART2','BODYPART3'],
        'skeleton': [['BODYPART1','BODYPART2'], ['BODYPART2','BODYPART3']],
        'anterior_bodyparts': ['BODYPART1'],
        'posterior_bodyparts': ['BODYPART3']})
        
    other = _update_dict(kwargs, {
        'session_name_suffix': '',
        'verbose':False,
        'conf_pseudocount': 1e-3,
        'video_dir': '',
        'keypoint_colormap': 'autumn',
        'whiten': True,
        'fix_heading': False,
        'seg_length': 10000 })
       
    fitting = _update_dict(kwargs, {
        'added_noise_level': 0.1,
        'PCA_fitting_num_frames': 1000000,
        'conf_threshold': 0.5,
#         'kappa_scan_target_duration': 12,
#         'kappa_scan_min': 1e2,
#         'kappa_scan_max': 1e12,
#         'num_arhmm_scan_iters': 50,
#         'num_arhmm_final_iters': 200,
#         'num_kpslds_scan_iters': 50,
#         'num_kpslds_final_iters': 500
    })
    
    comments = {
        'verbose': 'whether to print progress messages during fitting',
        'keypoint_colormap': 'colormap used for visualization; see `matplotlib.cm.get_cmap` for options',
        'added_noise_level': 'upper bound of uniform noise added to the data during initial AR-HMM fitting; this is used to regularize the model',
        'PCA_fitting_num_frames': 'number of frames used to fit the PCA model during initialization',
        'video_dir': 'directory with videos from which keypoints were derived (used for crowd movies)',
        'session_name_suffix': 'suffix used to match videos to session names; this can usually be left empty (see `util.find_matching_videos` for details)',
        'bodyparts': 'used to access columns in the keypoint data',
        'skeleton': 'used for visualization only',
        'use_bodyparts': 'determines the subset of bodyparts to use for modeling and the order in which they are represented',
        'anterior_bodyparts': 'used to initialize heading',
        'posterior_bodyparts': 'used to initialize heading',
        'seg_length': 'data are broken up into segments to parallelize fitting',
        'trans_hypparams': 'transition hyperparameters',
        'ar_hypparams': 'autoregressive hyperparameters',
        'obs_hypparams': 'keypoint observation hyperparameters',
        'cen_hypparams': 'centroid movement hyperparameters',
        'error_estimator': 'parameters to convert neural net likelihoods to error size priors',
        'save_every_n_iters': 'frequency for saving model snapshots during fitting; if 0 only final state is saved', 
        'kappa_scan_target_duration': 'target median syllable duration (in frames) for choosing kappa',
        'whiten': 'whether to whiten principal components; used to initialize the latent pose trajectory `x`',
        'conf_threshold': 'used to define outliers for interpolation when the model is initialized',
        'conf_pseudocount': 'pseudocount used regularize neural network confidences',
        'fix_heading': 'whether to keep the heading angle fixed; this should only be True if the pose is constrained to a narrow range of angles, e.g. a headfixed mouse.',
    }

    sections = [
        ('ANATOMY', anatomy),
        ('FITTING', fitting),
        ('HYPER PARAMS',hypperams),
        ('OTHER', other)
    ]

    with open(os.path.join(project_dir,'config.yml'),'w') as f: 
        f.write(_build_yaml(sections, comments))
                          
        
def check_config_validity(config):
    """
    Check if the config is valid.

    To be valid, the config must satisfy the following criteria:
        - All the elements of `config["use_bodyparts"]` are 
          also in `config["bodyparts"]` 
        - All the elements of `config["anterior_bodyparts"]` are
          also in `config["bodyparts"]` 
        - All the elements of `config["anterior_bodyparts"]` are
          also in `config["bodyparts"]` 
        - For each pair in `config["skeleton"]`, both elements 
          also in `config["bodyparts"]` 

    Parameters
    ----------
    config: dict 

    Returns
    -------
    validity: bool
    """
    error_messages = []
    
    # check anatomy
    for bodypart in config['use_bodyparts']:
        if not bodypart in config['bodyparts']:
            error_messages.append(           
                f'ACTION REQUIRED: `use_bodyparts` contains {bodypart} '
                'which is not one of the options in `bodyparts`.')
            
    for bodypart in sum(config['skeleton'],[]):
        if not bodypart in config['bodyparts']:
            error_messages.append(
                f'ACTION REQUIRED: `skeleton` contains {bodypart} '
                'which is not one of the options in `bodyparts`.')
            
    for bodypart in config['anterior_bodyparts']:
        if not bodypart in config['bodyparts']:
            error_messages.append(
                f'ACTION REQUIRED: `anterior_bodyparts` contains {bodypart} '
                'which is not one of the options in `bodyparts`.')
            
    for bodypart in config['posterior_bodyparts']:
        if not bodypart in config['bodyparts']:
            error_messages.append(     
                f'ACTION REQUIRED: `posterior_bodyparts` contains {bodypart} '
                'which is not one of the options in `bodyparts`.')

    if len(error_messages)==0: 
        return True
    for msg in error_messages: 
        print(fill(msg, width=70, subsequent_indent='  '), end='\n\n')
    return False
            

def load_config(project_dir, check_if_valid=True, build_indexes=True):
    """
    Load a project config file.
    
    Parameters
    ----------
    project_dir: str
        Directory containing the config file
        
    check_if_valid: bool, default=True
        Check if the config is valid using 
        :py:func:`keypoint_moseq.io.check_config_validity`
        
    build_indexes: bool, default=True
        Add keys `"anterior_idxs"` and `"posterior_idxs"` to the 
        config. Each maps to a jax array indexing the elements of 
        `config["anterior_bodyparts"]` and 
        `config["posterior_bodyparts"]` by their order in 
        `config["use_bodyparts"]`

    Returns
    -------
    config: dict
    """
    config_path = os.path.join(project_dir,'config.yml')
    
    with open(config_path, 'r') as stream:  
        config = yaml.safe_load(stream)

    if check_if_valid: 
        check_config_validity(config)
        
    if build_indexes:
        config['anterior_idxs'] = jnp.array(
            [config['use_bodyparts'].index(bp) for bp in config['anterior_bodyparts']])
        config['posterior_idxs'] = jnp.array(
            [config['use_bodyparts'].index(bp) for bp in config['posterior_bodyparts']])
    
    if not 'skeleton' in config or config['skeleton'] is None:
        config['skeleton'] = []
        
    return config


def update_config(project_dir, **kwargs):
    """
    Update the config file stored at `project_dir/config.yml`.
     
    Use keyword arguments to update key/value pairs in the config.
    To update model hyperparameters, just use the name of the 
    hyperparameter as the keyword argument. 

    Examples
    --------
    To update `video_dir` to `/path/to/videos`::

      >>> update_config(project_dir, video_dir='/path/to/videos')
      >>> print(load_config(project_dir)['video_dir'])
      /path/to/videos

    To update `trans_hypparams['kappa']` to `100`::

      >>> update_config(project_dir, kappa=100)
      >>> print(load_config(project_dir)['trans_hypparams']['kappa'])
      100
    """
    config = load_config(project_dir, check_if_valid=False, build_indexes=False)
    config.update(kwargs)
    generate_config(project_dir, **config)
    
        
def setup_project(project_dir, deeplabcut_config=None, sleap_file=None,
                  nwb_file=None, overwrite=False, **options):
    """
    Setup a project directory with the following structure::

        project_dir
        └── config.yml
    
    Parameters
    ----------
    project_dir: str 
        Path to the project directory (relative or absolute)
        
    deeplabcut_config: str, default=None
        Path to a deeplabcut config file. Will be used to initialize 
        `bodyparts`, `skeleton`, `use_bodyparts` and `video_dir` in 
        the keypoint MoSeq config. (overrided by kwargs). 

    sleap_file: str, default=None
        Path to a .hdf5 or .slp file containing predictions for one 
        video. Will be used to initialize `bodyparts`, `skeleton`, 
        and `use_bodyparts` in the keypoint MoSeq config. (overrided 
        by kwargs). 

    nwb_file: str, default=None
        Path to a .nwb file containing predictions for one video. 
        Will be used to initialize `bodyparts`, `skeleton`, and 
        `use_bodyparts` in the keypoint MoSeq config. (overrided 
        by kwargs). 
        
    overwrite: bool, default=False
        Overwrite any config.yml that already exists at the path
        `{project_dir}/config.yml`.
        
    options
        Used to initialize config file. Overrides default settings.
    """

    if os.path.exists(project_dir) and not overwrite:
        print(fill(
            f'The directory `{project_dir}` already exists. Use '
            '`overwrite=True` or pick a different name'))
        return
        
    if deeplabcut_config is not None: 
        dlc_options = {}
        with open(deeplabcut_config, 'r') as stream:           
            dlc_config = yaml.safe_load(stream)
            if dlc_config is None:
                raise RuntimeError(
                    f'{deeplabcut_config} does not exists or is not a'
                    ' valid yaml file')
            if 'multianimalproject' in dlc_config and dlc_config['multianimalproject']:
                dlc_options['bodyparts'] = dlc_config['multianimalbodyparts']
                dlc_options['use_bodyparts'] = dlc_config['multianimalbodyparts']
            else:
                dlc_options['bodyparts'] = dlc_config['bodyparts']
                dlc_options['use_bodyparts'] = dlc_config['bodyparts']
            dlc_options['skeleton'] = dlc_config['skeleton']
            dlc_options['video_dir'] = os.path.join(dlc_config['project_path'],'videos')
        options = {**dlc_options, **options}

    elif sleap_file is not None:
        sleap_options = {}
        if os.path.splitext(sleap_file)[1] == '.slp':
            slp_file = sleap_io.load_slp(sleap_file)
            assert len(slp_file.skeletons)==1, fill(
                f'{sleap_file} contains more than one skeleton. '
                'This is not currently supported. Please '
                'open a github issue or email calebsw@gmail.com')
            skeleton = slp_file.skeletons[0]
            node_names = skeleton.node_names
            edge_names = [[e.source.name, e.destination.name] for e in skeleton.edges]
        else:
            with h5py.File(sleap_file, 'r') as f:
                node_names = [n.decode('utf-8') for n in f['node_names']]
                edge_names = [[n.decode('utf-8') for n in edge] for edge in f['edge_names']]
        sleap_options['bodyparts'] = node_names
        sleap_options['use_bodyparts'] = node_names
        sleap_options['skeleton'] = edge_names
        options = {**sleap_options, **options}

    elif nwb_file is not None:
        nwb_options = {}
        with NWBHDF5IO(nwb_file, mode='r', load_namespaces=True) as io:
            pose_obj = _load_nwb_pose_obj(io)
            bodyparts = list(pose_obj.nodes[:])
            nwb_options['bodyparts'] = bodyparts
            nwb_options['use_bodyparts'] = bodyparts
            if 'edges' in pose_obj.fields:
                edges = pose_obj.edges[:]
                skeleton = [[bodyparts[i], bodyparts[j]] for i,j in edges]
                nwb_options['skeleton'] = skeleton
        options = {**nwb_options, **options}

    if not os.path.exists(project_dir):
        os.makedirs(project_dir)
    generate_config(project_dir, **options)
            

def save_pca(pca, project_dir, pca_path=None):
    """
    Save a PCA model to disk.

    The model is saved to `pca_path` or else to 
    `{project_dir}/pca.p`.
    
    Parameters
    ----------
    pca: :py:class:`sklearn.decomposition.PCA`
    project_dir: str
    pca_path: str, default=None
    """
    if pca_path is None: 
        pca_path = os.path.join(project_dir,'pca.p')
    joblib.dump(pca, pca_path)
    

def load_pca(project_dir, pca_path=None):
    """
    Load a PCA model from disk.

    The model is loaded from `pca_path` or else from 
    `{project_dir}/pca.p`.

    Parameters
    ----------
    project_dir: str
    pca_path: str, default=None

    Returns
    -------
    pca: :py:class:`sklearn.decomposition.PCA`
    """ 
    if pca_path is None:
        pca_path = os.path.join(project_dir,'pca.p')
        assert os.path.exists(pca_path), fill(
            f'No PCA model found at {pca_path}')
    return joblib.load(pca_path)


def load_checkpoint(project_dir=None, name=None, path=None):
    """
    Load model fitting checkpoint.

    The checkpoint path can be specified directly via `path` or else
    it is assumed to be `{project_dir}/<name>/checkpoint.p`.

    Parameters
    ----------
    project_dir: str, default=None
    name: str, default=None
    path: str, default=None

    Returns
    -------
    checkpoint: dict
        See :py:func:`keypoint_moseq.io.save_checkpoint`
    """
    path = _get_path(project_dir, name, path, 'checkpoint.p')
    return joblib.load(path)


def save_checkpoint(model, data, history, labels, iteration, 
                    path=None, name=None, project_dir=None,
                    save_history=True, save_states=True, save_data=True):
    """
    Save a checkpoint during model fitting.

    A single checkpoint file contains model snapshots from the full history
    of model fitting. To restart fitting from an iteration earlier than the
    last iteration, use :py:func:`keypoint_moseq.fitting.revert`.

    The checkpoint path can be specified directly via `path` or else
    it is assumed to be `{project_dir}/<name>/checkpoint.p`. See
    :py:func:`keypoint_moseq.fitting.fit_model` for a more detailed
    description of the checkpoint contents.

    Parameters
    ----------
    model: dict, history: dict
        See :py:func:`keypoint_moseq.fitting.fit_model`

    data: dict, labels: list of tuples
        See :py:func:`keypoint_moseq.io.format_data`

    iteration: int
        Current iteration of model fitting

    save_history: bool, default=True
        Whether to include `history` in the checkpoint

    save_states: bool, default=True
        Whether to include `states` in the checkpoint

    save_data: bool, default=True
        Whether to include `Y`, `conf`, and `mask` in the checkpoint
    
    project_dir: str, default=None
        Project directory; used in conjunction with `name` to determine
        the checkpoint path if `path` is not specified.

    name: str, default=None
        Model name; used in conjunction with `project_dir` to determine
        the checkpoint path if `path` is not specified.

    path: str, default=None
        Checkpoint path; if not specified, the checkpoint path is determined
        from `project_dir` and `name`.

    Returns
    -------
    checkpoint: dict
        Dictionary containing `history`, `labels` and `name` as 
        well as the key/value pairs from `model` and `data`.
    """
    path = _get_path(project_dir, name, path, 'checkpoint.p')

    dirname = os.path.dirname(path)
    if not os.path.exists(dirname): 
        print(fill(f'Creating the directory {dirname}'))
        os.makedirs(dirname)
    
    save_dict = {
        'labels': labels,
        'iteration' : iteration,
        'hypparams' : jax.device_get(model['hypparams']),
        'params'    : jax.device_get(model['params']), 
        'seed'      : np.array(model['seed']),
        'name'      : name}

    if save_data: 
        save_dict.update(jax.device_get(data))
        
    if save_states or save_data: 
        save_dict['mask'] = np.array(data['mask'])
        
    if save_states: 
        save_dict['states'] = jax.device_get(model['states'])
        save_dict['noise_prior'] = jax.device_get(model['noise_prior'])
        
    if save_history:
        save_dict['history'] = history
        
    joblib.dump(save_dict, path)
    return save_dict


def reindex_states_by_frequency(project_dir=None, name=None, path=None):
    """
    Reindex syllable labels by frequency in a saved checkpoint.

    This is an in-place operation: the checkpoint is loaded from disk,
    modified and saved to disk again. Reindexing effects the discrete
    state sequence `z` and autoregressive parameters (`Ab` and `Q`),
    and applies both to the current model and all saved snapshots. 

    The checkpoint path can be specified directly via `path` or else
    it is assumed to be `{project_dir}/<name>/checkpoint.p`.

    Parameters
    ----------
    project_dir: str, default=None
    name: str, default=None
    path: str, default=None    
    """
    path = _get_path(project_dir, name, path, 'checkpoint.p')
    checkpoint = joblib.load(path)

    

    
def load_results(project_dir=None, name=None, path=None):
    """
    Load the results from a modeled dataset.

    The results path can be specified directly via `path`. Otherwise
    it is assumed to be `{project_dir}/<name>/results.h5`.
    
    Parameters
    ----------
    project_dir: str, default=None
    name: str, default=None
    path: str, default=None

    Returns
    -------
    results: dict
        See :py:func:`keypoint_moseq.fitting.apply_model`
    """
    path = _get_path(project_dir, name, path, 'results.h5')
    return load_hdf5(path)


def save_results_as_csv(project_dir=None, name=None, h5_path=None, 
                        save_dir=None, use_bodyparts=None,
                        path_sep='-', **kwargs):
    """
    Convert modeling results from h5 to csv format.

    The input h5 file is assumed to contain modeling outputs for one
    or more recordings. This function creates a directory and then
    saves a separate csv file for each.

    The path to the input h5 file can be specified directly via
    `results_path`. Otherwise it is assumed to be
    `{project_dir}/{name}/results.h5`. The path to the output
    directory can be specified directly via `save_dir`. Otherwise
    it will be set to `{project_dir}/{name}/results`. Any files
    already in the output directory will be overwritten.
    
    Parameters
    ----------
    project_dir: str, default=None
        Project directory; required if `h5_path` or `save_dir` is not provided.

    name: str, default=None
        Name of the model; required if `h5_path` or `save_dir` is not provided.

    h5_path: str, default=None
        Path to the h5 file containing modeling results.

    save_dir: str, default=None
        Path to the directory where the csv files will be saved.

    use_bodyparts: list, default=None
        List of bodyparts that were used for modeling. If provided,
        will be used for the csv column names corresponding to 
        `est_coords`. Otherwise, the bodyparts will be named 
        `bodypart0`, `bodypart1` etc.

    path_sep: str, default='-'
        If a path separator ("/" or "\") is present in the recording name, 
        it will be replaced with `path_sep` when saving the csv file.
    """
    h5_path = _get_path(project_dir, name, h5_path, 'results.h5', 'h5_path')
    save_dir = _get_path(project_dir, name, h5_path, 'results', 'save_dir')

    if not os.path.exists(save_dir): 
        os.makedirs(save_dir)

    with h5py.File(h5_path, 'r') as results:
        for key in tqdm.tqdm(results.keys(), desc='Saving to csv'):
            column_names, data = [], []

            if 'syllable' in results[key].keys():
                column_names.append(['syllable'])
                data.append(results[key]['syllable'][()].reshape(-1,1))

            if 'centroid' in results[key].keys():
                d = results[key]['centroid'].shape[1]
                column_names.append(['centroid x', 'centroid y', 'centroid z'][:d])
                data.append(results[key]['centroid'][()])

            if 'heading' in results[key].keys():
                column_names.append(['heading'])
                data.append(results[key]['heading'][()].reshape(-1,1))

            if 'est_coords' in results[key].keys():
                k,d = results[key]['est_coords'].shape[1:]
                if use_bodyparts is None:
                    use_bodyparts = [f'bodypart{i}' for i in range(k)]
                for i, bp in enumerate(use_bodyparts):
                    column_names.append([f'est {bp} x', f'est {bp} y', f'{bp} z'][:d])
                    data.append(results[key]['est_coords'][:,i,:])

            if 'latent_state' in results[key].keys():
                latent_dim = results[key]['latent_state'].shape[1]
                column_names.append([f'latent_state {i}' for i in range(latent_dim)])
                data.append(results[key]['latent_state'][()])

            dfs = [pd.DataFrame(arr, columns=cols) for arr,cols in zip(data,column_names)]
            df = pd.concat(dfs, axis=1)

            for col in df.select_dtypes(include=[np.floating]).columns:
                df[col] = df[col].astype(float).round(4)

            save_name = key.replace(os.path.sep, path_sep)
            save_path = os.path.join(save_dir, save_name)
            df.to_csv(f'{save_path}.csv', index=False)


def _name_from_path(filepath, path_in_name, path_sep, remove_extension):
    """
    Create a name from a filepath. Either return the name of the file
    (with the extension removed) or return the full filepath, where the
    path separators are replaced with `path_sep`.
    """
    if remove_extension:
        filepath = os.path.splitext(filepath)[0]
    if path_in_name:
        return filepath.replace(os.path.sep, path_sep)
    else:
        return os.path.basename(filepath)


def load_keypoints(filepath_pattern, format, extension=None, recursive=True, 
                   path_sep='-', path_in_name=False, remove_extension=True):
    """
    Load keypoint tracking results from one or more files. Several file
    formats are supported:

    - deeplabcut
        .csv and .h5/.hdf5 files generated by deeplabcut. For single-animal
        tracking, each file yields a single key/value pair in the returned 
        `coordinates` and `confidences` dictionaries. For multi-animal tracking, 
        a key/vaue pair will be generated for each tracked individual. For 
        example the file `two_mice.h5` with individuals "mouseA" and "mouseB" 
        will yield the pair of keys `'two_mice_mouseA', 'two_mice_mouseB'`. 

    - sleap
        .slp and .h5/.hdf5 files generated by sleap. For single-animal tracking,
        each file yields a single key/value pair in the returned `coordinates` 
        and `confidences` dictionaries. For multi-animal tracking, a key/vaue
        pair will be generated for each track. For example a single file called 
        `two_mice.h5` will yield the pair of keys `'two_mice_track0', 
        'two_mice_track1'`.   

    - anipose
        .csv files generated by anipose. Each file should contain five columns
        per keypoint (x,y,z,error,score), plus a last column with the frame number.
        The `score` column is used as the keypoint confidence. 

    - sleap-anipose
        .h5/.hdf5 files generated by sleap-anipose. Each file should contain 
        a dataset called `'tracks'` with shape (n_frames, 1, n_keypoints, 3).
        If there is also a `'point_scores'` dataset, it will be used as the
        keypoint confidence. Otherwise, the confidence will be set to 1.

    - nwb
        .nwb files (Neurodata Without Borders). Each file should contain exactly
        one `PoseEstimation` object (for multi-animal tracking, each animal should
        be stored in its own .nwb file). The `PoseEstimation` object should contain
        one `PoseEstimationSeries` object for each bodypart. Confidence values are
        optional and will be set to 1 if not present.

    Parameters
    ----------
    filepath_pattern: str or list of str
        Filepath pattern for a set of deeplabcut csv or hdf5 files, 
        or a list of such patterns. Filepath patterns can be:

        - single file (e.g. `/path/to/file.csv`) 
        - single directory (e.g. `/path/to/dir/`)
        - set of files (e.g. `/path/to/fileprefix*`)
        - set of directories (e.g. `/path/to/dirprefix*`)

    format: str
        Format of the files to load. Must be one of `'deeplabcut'`, 
        `'sleap'`, `'anipose'`, or `'sleap-anipose'`.

    extension: str, default=None
        File extension to use when searching for files. If None, then
        the extension will be inferred from the `format` argument.
        - sleap: 'h5' or 'slp'
        - deeplabcut: 'csv' or 'h5'
        - anipose: 'csv'
        - sleap-anipose: 'h5'

    recursive: bool, default=True
        Whether to search recursively for deeplabcut csv or hdf5 files.

    path_in_name: bool, default=False
        Whether to name the tracking results from each file by the path
        to the file (True) or just the filename (False). If True, the
        `path_sep` argument is used to separate the path components.
        
    path_sep: str, default='-'
        Separator to use when `path_in_name` is True. For example,
        if `path_sep` is `'-'`, then the tracking results from the
        file `/path/to/file.csv` will be named `path-to-file`. Using
        `'/'` as the separator is discouraged, as it will cause problems
        saving/loading the modeling results to/from hdf5 files.

    remove_extension: bool, default=True
        Whether to remove the file extension when naming the tracking
        results from each file.

    Returns
    -------
    coordinates: dict
        Dictionary mapping filenames to keypoint coordinates as ndarrays
        of shape (n_frames, n_bodyparts, 2[or 3])

    confidences: dict
        Dictionary mapping filenames to `likelihood` scores as ndarrays
        of shape (n_frames, n_bodyparts)

    bodyparts: list of str
        List of bodypart names. The order of the names matches the order
        of the bodyparts in `coordinates` and `confidences`.
    """
    formats = ['deeplabcut', 'sleap', 'anipose', 'sleap-anipose', 'nwb']
    assert format in formats, fill(
        f'Unrecognized format {format}. Must be one of {formats}')
    
    if extension is None:
        extensions = {
            'deeplabcut'    : ['.csv','.h5','.hdf5'],
            'sleap'         : ['.h5','.hdf5','.slp'],
            'anipose'       : ['.csv'],
            'sleap-anipose' : ['.h5','.hdf5'],
            'nwb'           : ['.nwb']
        }[format]
    else:
        extensions = [extension]

    loader = {
        'deeplabcut'    : _deeplabcut_loader,
        'sleap'         : _sleap_loader,
        'anipose'       : _anipose_loader,
        'sleap-anipose' : _sleap_anipose_loader,
        'nwb'           : _nwb_loader,
    }[format]

    filepaths = list_files_with_exts(
        filepath_pattern, extensions, recursive=recursive)
    assert len(filepaths)>0, fill(
        f'No files with extensions {extensions} found for {filepath_pattern}')
    
    coordinates,confidences,bodyparts = {},{},None
    for filepath in tqdm.tqdm(filepaths, desc=f'Loading keypoints'):
        try:
            name = _name_from_path(filepath, path_in_name, path_sep, remove_extension)
            new_coordinates,new_confidences,bodyparts = loader(filepath, name)

            if set(new_coordinates.keys()) & set(coordinates.keys()):
                raise ValueError(fill(
                    f'Duplicate names found in {filepath_pattern}: '
                    f'{set(new_coordinates.keys()) & set(coordinates.keys())}. '
                    f'Please use `path_in_name=True` to avoid this error.'))
            
        except Exception as e:
            print(fill(f'Error loading {filepath}: {e}'))

        coordinates.update(new_coordinates)
        confidences.update(new_confidences)

    assert len(coordinates)>0, fill(
        f'No valid results found for {filepath_pattern}')

    check_nan_proportions(coordinates, bodyparts)
    return coordinates,confidences,bodyparts


def _deeplabcut_loader(filepath, name):
    """Load tracking results from deeplabcut csv or hdf5 files."""
    ext = os.path.splitext(filepath)[1]
    if ext=='.h5': df = pd.read_hdf(filepath)
    if ext=='.csv': df = pd.read_csv(filepath, header=[0,1,2], index_col=0) 

    coordinates,confidences = {},{}
    bodyparts = df.columns.get_level_values('bodyparts').unique().tolist()
    if 'individuals' in df.columns.names:
        for ind in df.columns.get_level_values('individuals').unique():
            ind_df = df.xs(ind, axis=1, level='individuals')
            arr = ind_df.to_numpy().reshape(len(ind_df), -1, 3)
            coordinates[f'{name}_{ind}'] = arr[:,:,:-1]
            confidences[f'{name}_{ind}'] = arr[:,:,-1]
    else:
        arr = df.to_numpy().reshape(len(df), -1, 3)
        coordinates[name] = arr[:,:,:-1]
        confidences[name] = arr[:,:,-1]

    return coordinates,confidences,bodyparts


def _sleap_loader(filepath, name):
    """Load keypoints from sleap hdf5 or slp files."""
    if os.path.splitext(filepath)[1] == '.slp':
        slp_file = sleap_io.load_slp(filepath)

        assert len(slp_file.skeletons)==1, fill(
            f'{filepath} contains more than one skeleton. '
            'This is not currently supported. Please '
            'open a github issue or email calebsw@gmail.com')
        
        bodyparts = slp_file.skeletons[0].node_names
        arr = slp_file.numpy(return_confidence=True)
        coords = arr[:,:,:-1]
        confs = arr[:,:,-1]
    else:
        with h5py.File(filepath, 'r') as f:
            coords = f['tracks'][()]
            confs = f['point_scores'][()]
            bodyparts = [name.decode('utf-8') for name in f['node_names']]

    if coords.shape[0] == 1: 
        coordinates = {name: coords[0].T}
        confidences = {name: confs[0].T}
    else:
        coordinates = {f'{name}_track{i}': coords[i].T for i in range(coords.shape[0])}
        confidences = {f'{name}_track{i}': confs[i].T for i in range(coords.shape[0])}
    return coordinates,confidences,bodyparts


def _anipose_loader(filepath, name):
    """Load keypoints from anipose csv files."""
    df = pd.read_csv(filepath,)
    bodyparts = [n.split('_x')[0] for n in df.columns[:-1][::5]] 
    arr = df.to_numpy()[:,:-1].reshape(len(df), len(bodyparts), 5)
    coordinates = {name: arr[:,:,:3]}
    confidences = {name: arr[:,:,4]}
    return coordinates,confidences,bodyparts


def _sleap_anipose_loader(filepath, name):
    """Load keypoints from sleap-anipose hdf5 files."""
    with h5py.File(filepath, 'r') as f:
        coords = f['tracks'][()]
        if 'point_scores' in f.keys():
            confs = f['point_scores'][()]
        else:
            confs = np.ones_like(coords[...,0])
        bodyparts = ['bodypart{}'.format(i) for i in range(coords.shape[2])]
        if coords.shape[1] == 1:
            coordinates = {name: coords[:,0]}
            confidences = {name: confs[:,0]}
        else:
            coordinates = {f'{name}_track{i}': coords[:,i] for i in range(coords.shape[1])}
            confidences = {f'{name}_track{i}': confs[:,i] for i in range(coords.shape[1])}
    return coordinates,confidences,bodyparts


def _load_nwb_pose_obj(io):
    """Grab PoseEstimation object from an opened .nwb file."""
    all_objs = io.read().all_children()
    pose_objs = [o for o in all_objs if isinstance(o, PoseEstimation)]
    assert len(pose_objs)>0, fill(
        f'No PoseEstimation objects found in {filepath}')
    assert len(pose_objs)==1, fill(
        f'Found multiple PoseEstimation objects in {filepath}. '
        'This is not currently supported. Please open a github '
        'issue to request this feature.')
    pose_obj = pose_objs[0]
    return pose_obj


def _nwb_loader(filepath, name):
    """Load keypoints from nwb files."""
    with NWBHDF5IO(filepath, mode='r', load_namespaces=True) as io:
        pose_obj = _load_nwb_pose_obj(io)
        bodyparts = list(pose_obj.nodes[:])
        coords = np.stack([pose_obj.pose_estimation_series[bp].data[()] for bp in bodyparts], axis=1)
        if 'confidence' in pose_obj.pose_estimation_series[bodyparts[0]].fields:
            confs = np.stack([pose_obj.pose_estimation_series[bp].confidence[()] for bp in bodyparts], axis=1)
        else: 
            confs = np.ones_like(coords[...,0])
        coordinates = {name: coords}
        confidences = {name: confs}
    return coordinates,confidences,bodyparts


# hdf5 save/load routines modified from
# https://gist.github.com/nirum/b119bbbd32d22facee3071210e08ecdf
def save_hdf5(filepath, save_dict):
    """
    Save a dict of pytrees to an hdf5 file.
    
    Parameters
    ----------
    filepath: str
        Path of the hdf5 file to create.

    save_dict: dict
        Dictionary where the values are pytrees, i.e. recursive 
        collections of tuples, lists, dicts, and numpy arrays.
    """
    with h5py.File(filepath, 'a') as f:
        for k,tree in save_dict.items():
            _savetree_hdf5(jax.device_get(tree), f, k)

def load_hdf5(filepath):
    """
    Load a dict of pytrees from an hdf5 file.

    Parameters
    ----------
    filepath: str
        Path of the hdf5 file to load.
            
    Returns
    -------
    save_dict: dict
        Dictionary where the values are pytrees, i.e. recursive
        collections of tuples, lists, dicts, and numpy arrays.
    """
    with h5py.File(filepath, 'r') as f:
        return {k:_loadtree_hdf5(f[k]) for k in f}

def _savetree_hdf5(tree, group, name):
    """Recursively save a pytree to an h5 file group."""
    if name in group: del group[name]
    if isinstance(tree, np.ndarray):
        group.create_dataset(name, data=tree)
    else:
        subgroup = group.create_group(name)
        subgroup.attrs['type'] = type(tree).__name__
        if isinstance(tree, tuple) or isinstance(tree, list):
            for k, subtree in enumerate(tree):
                _savetree_hdf5(subtree, subgroup, f'arr{k}')
        elif isinstance(tree, dict):
            for k, subtree in tree.items():
                _savetree_hdf5(subtree, subgroup, k)
        else: raise ValueError(f'Unrecognized type {type(tree)}')

def _loadtree_hdf5(leaf):
    """Recursively load a pytree from an h5 file group."""
    if isinstance(leaf, h5py.Dataset):
        return np.array(leaf)
    else:
        leaf_type = leaf.attrs['type']
        values = map(_loadtree_hdf5, leaf.values())
        if leaf_type == 'dict': return dict(zip(leaf.keys(), values))
        elif leaf_type == 'list': return list(values)
        elif leaf_type == 'tuple': return tuple(values)
        else: raise ValueError(f'Unrecognized type {leaf_type}')

