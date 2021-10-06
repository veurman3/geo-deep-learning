import argparse
import functools
import math
import multiprocessing
from collections import OrderedDict
import logging
import logging.config
from typing import List, Union

import numpy as np

np.random.seed(1234)  # Set random seed for reproducibility
import rasterio
import time

from pathlib import Path
from tqdm import tqdm
import geopandas as gpd

from utils.utils import get_key_def, read_csv, get_git_hash
from utils.readers import read_parameters
from utils.verifications import assert_crs_match, validate_features_from_gpkg, validate_raster
from solaris_gdl import tile
from solaris_gdl import vector

logging.getLogger(__name__)


def set_logging(console_level: str = 'WARNING', logfiles_dir=[str, Path], logfiles_prefix: str = 'log',
                conf_path: Union[str, Path] = 'utils/logging.conf'):
    """
    Configures logging with provided ".conf" file, console level, output paths.
    @param conf_path: Path to ".conf" file with loggers, handlers, formatters, etc.
    @param console_level: Level of logging to output to console. Defaults to "WARNING"
    @param logfiles_dir: path where output logs will be written
    @return:
    """
    conf_path = Path(conf_path).absolute()
    if not conf_path.is_file():
        raise FileNotFoundError(f'Invalid logging configuration file')
    log_config_path = Path(conf_path).absolute()
    out = Path(logfiles_dir) / logfiles_prefix
    logging.config.fileConfig(log_config_path, defaults={'logfilename': f'{out}.log',
                                                         'logfilename_error': f'{out}_error.log',
                                                         'logfilename_debug': f'{out}_debug.log',
                                                         'console_level': console_level})


def tiling_checker(src_img: Union[str, Path],
                   out_tiled_dir: Union[str, Path],
                   tile_size: int = 1024,
                   tile_stride: int = None,
                   out_suffix: str = '.tif',
                   verbose: bool = True):
    """
    Checks how many tiles should be created and compares with number of tiles already written to output directory
    @param src_img: path to source image
    @param out_tiled_dir: optional, path to output directory where tiles will be created
    @param tile_size: (int) optional, size of tile. Defaults to 1024.
    @param tile_stride: (int) optional, stride to use during tiling. Defaults to tile_size.
    @param out_suffix: optional, suffix of output tiles (ex.: ".tif" or ".geojson"). Defaults to ".tif"
    @return: number of actual tiles in output directory, number of expected tiles
    """
    tile_stride = tile_size if not tile_stride else tile_stride
    metadata = rasterio.open(src_img).meta
    tiles_x = 1 + math.ceil((metadata['width'] - tile_size) / tile_stride)
    tiles_y = 1 + math.ceil((metadata['height'] - tile_size) / tile_stride)
    nb_exp_tiles = tiles_x * tiles_y
    nb_act_tiles = len(list(out_tiled_dir.glob(f'*{out_suffix}')))
    if verbose:
        logging.info(f'Number of actual tiles with suffix "{out_suffix}": {nb_act_tiles}\n'
                     f'Number of expected tiles : {nb_exp_tiles}\n')
    return nb_act_tiles, nb_exp_tiles


def map_wrapper(x):
    '''For multi-threading'''
    return x[0](*(x[1:]))


def out_tiling_dir(root, dataset, aoi_name, category):
    root = Path(root)
    return root / dataset.strip() / aoi_name.strip() / category


def get_src_tile_size(dest_tile_size, resize_factor: float = None):
    """
    Outputs dimension of source tile if resizing, given destination size and resizing factor
    @param dest_tile_size: (int) Size of tile that is expected as output
    @param resize_factor: (float) Resize factor to apply to source imagery before outputting tiles
    @return: (int) Source tile size
    """
    if resize_factor is not None and dest_tile_size % resize_factor != 0:
        raise ValueError(f'Destination tile size "{dest_tile_size}" must be divisible by resize "{resize_factor}"')
    elif resize_factor:
        src_tile_size = int(dest_tile_size / resize_factor)
    else:
        src_tile_size = dest_tile_size
    return src_tile_size


def tiling(src_img: Union[str, Path],
           out_img_dir: Union[str, Path],
           tile_size: int = 1024,
           bands_idxs: List = None,
           resize: int = 1,
           out_label_dir: Union[str, Path] = None,
           src_label: Union[str, Path] = None):
    """
    Calls solaris_gdl tiling function and outputs tiles in output directories
    @param src_img: path to source image
    @param out_img_dir: path to output tiled images directory
    @param tile_size: optional, size of tiles to output. Defaults to 1024
    @param bands_idxs:
    @param resize: (float) optional, Multiple by which source imagery must be resampled. Destination size must be divisible by this multiple without remainder. Rasterio will use bilinear resampling. Defaults to 1 (no resampling).
    @param out_label_dir: optional, path to output tiled images directory
    @param src_label: optional, path to source label (must be a geopandas compatible format like gpkg or geojson)
    @return: written tiles to output directories as .tif for imagery and .geojson for label.
    """
    src_tile_size = get_src_tile_size(tile_size, resize)
    raster_tiler = tile.raster_tile.RasterTiler(dest_dir=out_img_dir,
                                                src_tile_size=(src_tile_size, src_tile_size),
                                                dest_tile_size=(tile_size, tile_size),
                                                resize=resize,
                                                alpha=False,
                                                verbose=True)
    raster_bounds_crs = raster_tiler.tile(src_img, channel_idxs=bands_idxs)
    if out_label_dir and src_label is not None:
        vector_tiler = tile.vector_tile.VectorTiler(dest_dir=out_label_dir, verbose=True)
        vector_tiler.tile(src_label, tile_bounds=raster_tiler.tile_bounds, tile_bounds_crs=raster_bounds_crs)


def filter_gdf(gdf: gpd.GeoDataFrame, attr_field: str = None, attr_vals: List = None):
    """
    Filter features from a geopandas.GeoDataFrame according to an attribute field and filtering values
    @param gdf: gpd.GeoDataFrame to filter feature from
    @param attr_field: Name of field on which filtering operation is based
    @param attr_vals: list of integer values to keep in filtered GeoDataFrame
    @return: Subset of source GeoDataFrame with only filtered features (deep copy)
    """
    logging.debug(gdf.columns)
    if not attr_field or not attr_vals:
        return gdf
    if not attr_field in gdf.columns:
        attr_field = attr_field.split('/')[-1]
    try:
        condList = [gdf[f'{attr_field}'] == val for val in attr_vals]
        condList.extend([gdf[f'{attr_field}'] == str(val) for val in attr_vals])
        allcond = functools.reduce(lambda x, y: x | y, condList)  # combine all conditions with OR
        gdf_filtered = gdf[allcond].copy(deep=True)
        return gdf_filtered
    except KeyError as e:
        logging.error(f'Column "{attr_field}" not found in label file {gdf.info()}')
        return gdf


def main(params):
    """
    Training and validation datasets preparation.

    Process
    -------
    1. Read csv file and validate existence of all input files and GeoPackages.

    2. Do the following verifications:
        1. Assert number of bands found in raster is equal to desired number
           of bands.
        2. Check that `num_classes` is equal to number of classes detected in
           the specified attribute for each GeoPackage.
           Warning: this validation will not succeed if a Geopackage
                    contains only a subset of `num_classes` (e.g. 3 of 4).
        3. Assert Coordinate reference system between raster and gpkg match.

    3. For each line in the csv file, output tiles from imagery and label files based on "samples_size" parameter
    N.B. This step can be parallelized with multiprocessing. Tiling will be skipped if tiles already exist.

    4. Create pixels masks from each geojson tile and write a list of image tile / pixelized label tile to text file
    N.B. for train/val datasets, only tiles that pass the "min_annot_percent" threshold are kept.

    -------
    :param params: (dict) Parameters found in the yaml config file.
    """
    start_time = time.time()

    # MANDATORY PARAMETERS
    num_classes = get_key_def('num_classes', params['global'], expected_type=int)
    num_bands = get_key_def('number_of_bands', params['global'], expected_type=int)
    csv_file = get_key_def('prep_csv_file', params['sample'], expected_type=str)

    # OPTIONAL PARAMETERS

    # mlflow logging
    mlflow_uri = get_key_def('mlflow_uri', params['global'], default="./mlruns")
    experiment_name = get_key_def('mlflow_experiment_name', params['global'], default=f'{Path(csv_file).stem}',
                                  expected_type=str)

    # basics
    debug = get_key_def('debug_mode', params['global'], False)
    task = get_key_def('task', params['global'], 'segmentation', expected_type=str)
    if task == 'classification':
        raise ValueError(f"Got task {task}. Expected 'segmentation'.")
    elif not task == 'segmentation':
        raise ValueError(f"images_to_samples.py isn't necessary for classification tasks")
    val_percent = get_key_def('val_percent', params['sample'], default=10, expected_type=int)
    bands_idxs = get_key_def('bands_idxs', params['global'], default=None, expected_type=List)
    if bands_idxs is not None and not len(bands_idxs) == num_bands:
        raise ValueError(f"List of band indexes should be of same length as num_bands.\n"
                         f"Bands_idxs: {bands_idxs}\n"
                         f"num_bands: {num_bands}")
    resize = get_key_def('resize', params['sample'], default=1)
    parallel = get_key_def('parallelize_tiling', params['sample'], default=False, expected_type=bool)

    # parameters to set output tiles directory
    data_path = Path(get_key_def('data_path', params['global'], f'./data', expected_type=str))
    Path.mkdir(data_path, exist_ok=True, parents=True)
    samples_size = get_key_def("samples_size", params["global"], default=1024, expected_type=int)
    if 'sampling_method' not in params['sample'].keys():
        params['sample']['sampling_method'] = {}
    min_annot_perc = get_key_def('min_annotated_percent', params['sample']['sampling_method'], default=0,
                                 expected_type=int)
    min_raster_tile_size = get_key_def('min_raster_tile_size', params['sample'], default=0, expected_type=int)
    if not data_path.is_dir():
        raise FileNotFoundError(f'Could not locate data path {data_path}')
    samples_folder_name = (f'tiles{samples_size}_min-annot{min_annot_perc}_{num_bands}bands')
    attr_vals = get_key_def('target_ids', params['sample'], None, expected_type=List)

    # add git hash from current commit to parameters if available. Parameters will be saved to hdf5s
    params['global']['git_hash'] = get_git_hash()

    list_data_prep = read_csv(csv_file)

    smpls_dir = data_path / experiment_name / samples_folder_name
    if smpls_dir.is_dir():
        print(f'WARNING: Data path exists: {smpls_dir}. Make sure samples belong to the same experiment.')
    Path.mkdir(smpls_dir, exist_ok=True, parents=True)

    # See: https://docs.python.org/2.4/lib/logging-config-fileformat.html
    console_level_logging = 'INFO' if not debug else 'DEBUG'
    set_logging(console_level=console_level_logging, logfiles_dir=smpls_dir, logfiles_prefix=samples_folder_name)

    if debug:
        logging.warning(f'Debug mode activated. Some debug features may mobilize extra disk space and '
                        f'cause delays in execution.')

    logging.info(f'\n\tSuccessfully read csv file: {Path(csv_file).name}\n'
                 f'\tNumber of rows: {len(list_data_prep)}\n'
                 f'\tCopying first entry:\n{list_data_prep[0]}\n')

    logging.info(f'Samples will be written to {smpls_dir}\n\n')

    # Assert that all items in target_ids are integers (ex.: single-class samples from multi-class label)
    if attr_vals:
        for item in attr_vals:
            if not isinstance(item, int):
                raise logging.error(ValueError(f'Target id "{item}" in target_ids is {type(item)}, expected int.'))

    # TODO: move validation steps to validate_geodata.py
    # VALIDATION: (1) Assert num_classes parameters == num actual classes in gpkg and (2) check CRS match (tif and gpkg)
    valid_gpkg_set = set()
    no_gt = False
    for info in tqdm(list_data_prep, position=0):
        _, metadata = validate_raster(info['tif'])
        if metadata['count'] > num_bands and not bands_idxs:
            raise ValueError(f'Missing band indexes to keep. Imagery contains {metadata["count"]} bands. '
                             f'Number of bands to be kept in tiles {num_bands}')
        elif metadata['count'] < num_bands:
            raise ValueError(f'Imagery contains {metadata["count"]} bands. "num_bands" is {num_bands}\n'
                             f'Expected {num_bands} or more bands in source imagery')
        if info['gpkg']:
            if info['gpkg'] not in valid_gpkg_set:
                # FIXME: check/fix this validation and use it
                #gpkg_classes = validate_num_classes(info['gpkg'], num_classes, info['attribute_name'],
                #                                    target_ids=attr_vals)
                assert_crs_match(info['tif'], info['gpkg'])
                valid_gpkg_set.add(info['gpkg'])
        else:
            logging.warning(f"No ground truth data found for {info['tif']}. Only imagery will be processed from now on")
            no_gt = True
        if not info['dataset'] in ['trn', 'tst']:
            raise ValueError(f'Dataset value must be "trn" or "tst". Got: {info["dataset"]}')

    if debug:
        # VALIDATION (debug only): Checking validity of features in vector files
        for info in tqdm(list_data_prep, position=0, desc=f"Checking validity of features in vector files"):
            # TODO: make unit to test this with invalid features.
            if not no_gt:
                invalid_features = validate_features_from_gpkg(info['gpkg'], info['attribute_name'])
                if invalid_features:
                    logging.critical(f"{info['gpkg']}: Invalid geometry object(s) '{invalid_features}'")

    datasets = ['trn', 'val', 'tst']

    # For each row in csv: (1) burn vector file to raster, (2) read input raster image, (3) prepare samples
    input_args = []
    logging.info(f"Preparing samples \n\tSamples_size: {samples_size} ")
    for info in tqdm(list_data_prep, position=0, leave=False):
        try:
            aoi_name = Path(info['tif']).stem if not info['aoi'] else info['aoi']
            # FIXME: why does output dir change whether GT is present or not?
            out_img_dir = out_tiling_dir(smpls_dir, info['dataset'], aoi_name, 'sat_img')
            out_gt_dir = out_tiling_dir(smpls_dir, info['dataset'], aoi_name, 'map_img') if not no_gt else None

            do_tile = True
            act_img_tiles, exp_tiles = tiling_checker(info['tif'], out_img_dir,
                                                      tile_size=samples_size, out_suffix=('.tif'))
            if no_gt:
                if act_img_tiles == exp_tiles:
                    logging.info(f'All {exp_tiles} tiles exist. Skipping tiling.\n')
                    do_tile = False
                elif act_img_tiles > exp_tiles:
                    logging.critical(f'\nToo many tiles for "{info["tif"]}". \n'
                                     f'Expected: {exp_tiles}\n'
                                     f'Actual image tiles: {act_img_tiles}\n'
                                     f'Skipping tiling.')
                elif act_img_tiles > 0:
                    logging.critical(f'Missing tiles for {info["tif"]}. \n'
                                     f'Expected: {exp_tiles}\n'
                                     f'Actual image tiles: {act_img_tiles}\n'
                                     f'Starting tiling from scratch...')
                else:
                    logging.debug(f'Expected: {exp_tiles}\n'
                                  f'Actual image tiles: {act_img_tiles}\n'
                                  f'Starting tiling from scratch...')
            else:
                act_gt_tiles, _ = tiling_checker(info['tif'], out_gt_dir,
                                                 tile_size=samples_size, out_suffix=('.geojson'))
                if act_img_tiles == act_gt_tiles == exp_tiles:
                    logging.info('All tiles exist. Skipping tiling.\n')
                    do_tile = False
                elif act_img_tiles > exp_tiles and act_gt_tiles > exp_tiles:
                    logging.critical(f'\nToo many tiles for "{info["tif"]}". \n'
                                     f'Expected: {exp_tiles}\n'
                                     f'Actual image tiles: {act_img_tiles}\n'
                                     f'Actual label tiles: {act_gt_tiles}\n'
                                     f'Skipping tiling.')
                    do_tile = False
                elif act_img_tiles > 0 or act_gt_tiles > 0:
                    logging.critical('Missing tiles for {info["tif"]}. \n'
                                     f'Expected: {exp_tiles}\n'
                                     f'Actual image tiles: {act_img_tiles}\n'
                                     f'Actual label tiles: {act_gt_tiles}\n'
                                     f'Starting tiling from scratch...')
                else:
                    logging.debug(f'Expected: {exp_tiles}\n'
                                  f'Actual image tiles: {act_img_tiles}\n'
                                  f'Actual label tiles: {act_gt_tiles}\n'
                                  f'Starting tiling from scratch...')

            # if no previous step has shown existence of all tiles, then go on and tile.
            if do_tile:
                if parallel:
                    input_args.append([tiling, info['tif'], out_img_dir, samples_size, bands_idxs, resize, out_gt_dir,
                                       info['gpkg']])
                else:
                    tiling(info['tif'], out_img_dir, samples_size, bands_idxs, resize, out_gt_dir, info['gpkg'])

        except OSError:
            logging.exception(f'An error occurred while preparing samples with "{Path(info["tif"]).stem}" (tiff) and '
                              f'{Path(info["gpkg"]).stem} (gpkg).')
            continue

    if parallel:
        logging.info(f'Will tile {len(input_args)} images and labels...')
        with multiprocessing.get_context('spawn').Pool(None) as pool:
            pool.map(map_wrapper, input_args)

    logging.info(f"Tiling done. Creating pixel masks from clipped geojsons...\n"
                 f"Validation set: {val_percent} % of created training tiles")
    dataset_files = {dataset: smpls_dir / f'{experiment_name}_{dataset}.txt' for dataset in datasets}
    for file in dataset_files.values():
        if file.is_file():
            logging.critical(f'Dataset list exists and will be overwritten: {file}')
            file.unlink()

    datasets_kept = {dataset: 0 for dataset in datasets}
    datasets_total = {dataset: 0 for dataset in datasets}
    # loop through line of csv again
    for info in tqdm(list_data_prep, position=0, desc='Filtering tiles and writing list to dataset text files'):
        # FIXME: create Tiler class to prevent redundance here.
        aoi_name = Path(info['tif']).stem if not info['aoi'] else info['aoi']
        out_img_dir = out_tiling_dir(smpls_dir, info['dataset'], aoi_name, 'sat_img')
        out_gt_dir = out_tiling_dir(smpls_dir, info['dataset'], aoi_name, 'map_img')
        imgs_tiled = sorted(list(out_img_dir.glob('*.tif')))
        gts_tiled = sorted(list(out_gt_dir.glob('*.geojson')))
        if debug:
            for sat_img_tile in tqdm(imgs_tiled, desc='DEBUG: Checking if imagery tiles are valid'):
                is_valid, _ = validate_raster(sat_img_tile)
                if not is_valid:
                    logging.error(f'Invalid imagery tile: {sat_img_tile}')
            for map_img_tile in tqdm(gts_tiled, desc='DEBUG: Checking if ground truth tiles are valid'):
                try:
                    gpd.read_file(map_img_tile)
                except Exception as e:
                    logging.error(f'Invalid ground truth tile: {sat_img_tile}. Error: {e}')
        if len(imgs_tiled) > 0 and len(gts_tiled) == 0:
            logging.warning('List of training tiles contains no ground truth, only imagery.')
            for sat_img_tile in imgs_tiled:
                sat_size = sat_img_tile.stat().st_size
                if sat_size < min_raster_tile_size:
                    logging.debug(f'File {sat_img_tile} below minimum size ({min_raster_tile_size}): {sat_size}')
                    continue
                dataset = sat_img_tile.parts[-4]
                with open(dataset_files[dataset], 'a') as dataset_file:
                    dataset_file.write(f'{sat_img_tile.absolute()}\n')
        elif not len(imgs_tiled) == len(gts_tiled):
            msg = f"Number of imagery tiles ({len(imgs_tiled)}) and label tiles ({len(gts_tiled)}) don't match"
            logging.error(msg)
            raise IOError(msg)
        else:
            for sat_img_tile, map_img_tile in zip(imgs_tiled, gts_tiled):
                sat_size = sat_img_tile.stat().st_size
                if sat_size < min_raster_tile_size:
                    logging.debug(f'File {sat_img_tile} below minimum size ({min_raster_tile_size}): {sat_size}')
                    continue
                dataset = sat_img_tile.parts[-4]
                attr_field = info['attribute_name']
                out_px_mask = map_img_tile.parent / f'{map_img_tile.stem}.tif'
                logging.debug(map_img_tile)
                gdf = gpd.read_file(map_img_tile)
                burn_field = None
                gdf_filtered = filter_gdf(gdf, attr_field, attr_vals)

                sat_tile_fh = rasterio.open(sat_img_tile)
                sat_tile_ext = abs(sat_tile_fh.bounds.right - sat_tile_fh.bounds.left) * \
                               abs(sat_tile_fh.bounds.top - sat_tile_fh.bounds.bottom)
                annot_ct_vec = gdf_filtered.area.sum()
                annot_perc = annot_ct_vec / sat_tile_ext
                if dataset in ['trn', 'train']:
                    if annot_perc * 100 >= min_annot_perc:
                        random_val = np.random.randint(1, 100)
                        dataset = 'val' if random_val < val_percent else dataset
                        vector.mask.footprint_mask(df=gdf_filtered, out_file=str(out_px_mask),
                                                       reference_im=str(sat_img_tile),
                                                       burn_field=burn_field)
                        with open(dataset_files[dataset], 'a') as dataset_file:
                            dataset_file.write(f'{sat_img_tile.absolute()} {out_px_mask.absolute()} '
                                               f'{int(annot_perc * 100)}\n')
                        datasets_kept[dataset] += 1
                    datasets_total[dataset] += 1
                elif dataset in ['tst', 'test']:
                    vector.mask.footprint_mask(df=gdf_filtered, out_file=str(out_px_mask),
                                                   reference_im=str(sat_img_tile),
                                                   burn_field=burn_field)
                    with open(dataset_files[dataset], 'a') as dataset_file:
                        dataset_file.write(f'{sat_img_tile.absolute()} {out_px_mask.absolute()} '
                                           f'{int(annot_perc * 100)}\n')
                    datasets_kept[dataset] += 1
                    datasets_total[dataset] += 1
                else:
                    logging.error(f"Invalid dataset value {dataset} for {sat_img_tile}")

    for dataset in datasets:
        if dataset == 'train':
            logging.info(f"\nDataset: {dataset}\n"
                         f"Number of tiles with non-zero values above {min_annot_perc}%: \n"
                         f"\t Train set: {datasets_kept[dataset]}\n"
                         f"\t Validation set: {datasets_kept['val']}\n"
                         f"Number of total tiles created: {datasets_total[dataset]}\n")
        elif dataset == 'test':
            logging.info(f"\nDataset: {dataset}\n"
                         f"Number of total tiles created: {datasets_total[dataset]}\n")
    logging.info(f"End of process. Elapsed time: {int(time.time() - start_time)} seconds")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sample preparation')
    input_type = parser.add_mutually_exclusive_group(required=True)
    input_type.add_argument('-c', '--csv', metavar='csv_file', help='Path to csv containing listed geodata with columns'
                                                                    ' as expected by geo-deep-learning. See README')
    input_type.add_argument('-p', '--param', metavar='yaml_file', help='Path to parameters stored in yaml')
    # FIXME: use hydra to better function if yaml is also used.
    parser.add_argument('--resize', default=1)
    parser.add_argument('--bands', default=None)
    # FIXME: enable BooleanOptionalAction only when GDL has moved to Python 3.8
    parser.add_argument('--debug', metavar='debug_mode', #action=argparse.BooleanOptionalAction,
                        default=False)
    parser.add_argument('--parallel', metavar='multiprocessing', #action=argparse.BooleanOptionalAction,
                        default=False,
                        help="Boolean. If activated, will use python's multiprocessing package to parallelize")
    args = parser.parse_args()
    if args.param:
        params = read_parameters(args.param)
    elif args.csv:
        data_list = read_csv(args.csv)
        params = OrderedDict()
        params['global'] = OrderedDict()
        params['global']['debug_mode'] = args.debug
        bands_per_imagery = []
        classes_per_gt_file = []
        for data in data_list:
            with rasterio.open(data['tif'], 'r') as rdataset:
                _, metadata = validate_raster(data['tif'])
                bands_per_imagery.append(metadata['count'])
        if len(set(bands_per_imagery)) == 1:
            params['global']['number_of_bands'] = int(list(set(bands_per_imagery))[0])
            print(f"Inputted imagery contains {params['global']['number_of_bands']} bands")
        else:
            raise ValueError(f'Not all imagery has identical number of bands: {bands_per_imagery}')
        for data in data_list:
            if data['gpkg']:
                attr_field = data['attribute_name'].split('/')[-1]
                gdf = gpd.read_file(data['gpkg'])
                classes_per_gt_file.append(len(set(gdf[f'{attr_field}'])))
                print(f'Number of classes in ground truth files for attribute {attr_field}:'
                      f'\n{classes_per_gt_file}\n'
                      f'Min: {min(classes_per_gt_file)}\n'
                      f'Max: {max(classes_per_gt_file)}\n'
                      f'Number of classes will be set to max value.')
        params['global']['num_classes'] = max(classes_per_gt_file) if classes_per_gt_file else None
        params['sample'] = OrderedDict()
        params['sample']['parallelize_tiling'] = args.parallel
        params['sample']['prep_csv_file'] = args.csv

        if args.resize:
            params['sample']['resize'] = args.resize
        if args.bands:
            params['global']['bands_idxs'] = args.bands

    print(f'\n\nStarting data to tiles preparation with {args}\n\n')
    main(params)