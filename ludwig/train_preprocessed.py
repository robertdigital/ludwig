#! /usr/bin/env python
# coding=utf-8
# Copyright (c) 2019 Uber Technologies, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import logging
import os
import sys
from pprint import pformat

import yaml

from ludwig.contrib import contrib_command
from ludwig.train import update_model_definition_with_metadata
from ludwig.train import get_experiment_dir_name
from ludwig.train import get_file_names
from ludwig.train import train
from ludwig.data.preprocessing import load_metadata
from ludwig.data.parquet_dataset import ParquetDataset
from ludwig.globals import LUDWIG_VERSION, set_on_master, is_on_master
from ludwig.globals import TRAIN_SET_METADATA_FILE_NAME
from ludwig.models.modules.measure_modules import get_best_function
from ludwig.utils.data_utils import save_json
from ludwig.utils.defaults import merge_with_defaults
from ludwig.utils.misc import get_experiment_description
from ludwig.utils.print_utils import logging_level_registry
from ludwig.utils.print_utils import print_ludwig

logger = logging.getLogger(__name__)


def train_preprocessed(
    model_definition,
    train_parquet_path,
    validation_parquet_path,
    test_parquet_path,
    model_definition_file,
    train_set_metadata_json,
    experiment_name='experiment',
    model_name='run',
    model_load_path=None,
    model_resume_path=None,
    skip_save_training_description=False,
    skip_save_training_statistics=False,
    skip_save_model=False,
    skip_save_progress=False,
    skip_save_log=False,
    output_directory='results',
    should_close_session=True,
    gpus=None,
    gpu_fraction=1.0,
    use_horovod=False,
    random_seed=42,
    debug=False,
    **kwargs
):
    """*full_train* defines the entire training procedure used by Ludwig's
    internals. Requires most of the parameters that are taken into the model.
    Builds a full ludwig model and performs the training.

    :param model_definition: Model definition which defines the different
           parameters of the model, features, preprocessing and training.
    :type model_definition: Dictionary
    :param train_parquet_path Path to the preprocessed training data stored
            as parquet
    :type train_parquet_path: filepath(str)
    :param validation_parquet_path Path to the preprocessed training data stored
            as parquet
    :type validation_parquet_path: filepath(str)
    :param test_parquet_path Path to the preprocessed training data stored
            as parquet
    :type test_parquet_path: filepath(str)
    :param model_definition_file: The file that specifies the model definition.
           It is a yaml file.
    :type model_definition_file: filepath (str)
    :param data_csv: A CSV file contanining the input data which is used to
           train, validate and test a model. The CSV either contains a
           split column or will be split.
    :type train_set_metadata_json: filepath (str)
    :param experiment_name: The name for the experiment.
    :type experiment_name: Str
    :param model_name: Name of the model that is being used.
    :type model_name: Str
    :param model_load_path: If this is specified the loaded model will be used
           as initialization (useful for transfer learning).
    :type model_load_path: filepath (str)
    :param model_resume_path: Resumes training of the model from the path
           specified. The difference with model_load_path is that also training
           statistics like the current epoch and the loss and performance so
           far are also resumed effectively cotinuing a previously interrupted
           training process.
    :type model_resume_path: filepath (str)
    :param skip_save_model: Disables
               saving model weights and hyperparameters each time the model
           improves. By default Ludwig saves model weights after each epoch
           the validation measure imrpvoes, but if the model is really big
           that can be time consuming if you do not want to keep
           the weights and just find out what performance can a model get
           with a set of hyperparameters, use this parameter to skip it,
           but the model will not be loadable later on.
    :type skip_save_model: Boolean
    :param skip_save_progress: Disables saving
           progress each epoch. By default Ludwig saves weights and stats
           after each epoch for enabling resuming of training, but if
           the model is really big that can be time consuming and will uses
           twice as much space, use this parameter to skip it, but training
           cannot be resumed later on.
    :type skip_save_progress: Boolean
    :param skip_save_processed_input: If a CSV dataset is provided it is
           preprocessed and then saved as an hdf5 and json to avoid running
           the preprocessing again. If this parameter is False,
           the hdf5 and json file are not saved.
    :type skip_save_processed_input: Boolean
    :param skip_save_log: Disables saving TensorBoard
           logs. By default Ludwig saves logs for the TensorBoard, but if it
           is not needed turning it off can slightly increase the
           overall speed..
    :type skip_save_progress: Boolean
    :param output_directory: The directory that will contanin the training
           statistics, the saved model and the training procgress files.
    :type output_directory: filepath (str)
    :param gpus: List of GPUs that are available for training.
    :type gpus: List
    :param gpu_fraction: Fraction of the memory of each GPU to use at
           the beginning of the training. The memory may grow elastically.
    :type gpu_fraction: Integer
    :param random_seed: Random seed used for weights initialization,
           splits and any other random function.
    :type random_seed: Integer
    :param debug: If true turns on tfdbg with inf_or_nan checks.
    :type debug: Boolean
    :returns: None
    """
    # set input features defaults
    if model_definition_file is not None:
        with open(model_definition_file, 'r') as def_file:
            model_definition = merge_with_defaults(yaml.safe_load(def_file))
    else:
        model_definition = merge_with_defaults(model_definition)

    # setup directories and file names
    experiment_dir_name = None
    if model_resume_path is not None:
        if os.path.exists(model_resume_path):
            experiment_dir_name = model_resume_path
        else:
            if is_on_master():
                logger.info(
                    'Model resume path does not exists, '
                    'starting training from scratch'
                )
            model_resume_path = None

    if model_resume_path is None:
        if is_on_master():
            experiment_dir_name = get_experiment_dir_name(
                output_directory,
                experiment_name,
                model_name
            )
        else:
            experiment_dir_name = '/'

    # if model_load_path is not None, load its train_set_metadata
    if model_load_path is not None:
        train_set_metadata_json = os.path.join(
            model_load_path,
            TRAIN_SET_METADATA_FILE_NAME
        )

    # if we are skipping all saving,
    # there is no need to create a directory that will remain empty
    should_create_exp_dir = not (
            skip_save_training_description and
            skip_save_training_statistics and
            skip_save_model and
            skip_save_progress and
            skip_save_log
    )
    if is_on_master():
        if should_create_exp_dir:
            if not os.path.exists(experiment_dir_name):
                os.makedirs(experiment_dir_name)

    description_fn, training_stats_fn, model_dir = get_file_names(
        experiment_dir_name
    )

    # save description
    description = get_experiment_description(
        model_definition,
        metadata_json=train_set_metadata_json,
        random_seed=random_seed
    )
    if is_on_master():
        save_json(description_fn, description)
        # print description
        logger.info('Experiment name: {}'.format(experiment_name))
        logger.info('Model name: {}'.format(model_name))
        logger.info('Output path: {}'.format(experiment_dir_name))
        logger.info('\n')
        for key, value in description.items():
            logger.info('{}: {}'.format(key, pformat(value, indent=4)))
        logger.info('\n')

    training_set = ParquetDataset(
        {},
        model_definition['input_features'],
        model_definition['output_features'],
        train_parquet_path
    )
    validation_set = ParquetDataset(
        {},
        model_definition['input_features'],
        model_definition['output_features'],
        validation_parquet_path
    )
    test_set = ParquetDataset(
        {},
        model_definition['input_features'],
        model_definition['output_features'],
        test_parquet_path
    )
    train_set_metadata = load_metadata(train_set_metadata_json)

    if is_on_master():
        logger.info('Training set: {0}'.format(training_set.size))
        if validation_set is not None:
            logger.info('Validation set: {0}'.format(validation_set.size))
        if test_set is not None:
            logger.info('Test set: {0}'.format(test_set.size))

    # update model definition with metadata properties
    update_model_definition_with_metadata(
        model_definition,
        train_set_metadata
    )

    if is_on_master():
        if not skip_save_model:
            # save train set metadata
            os.makedirs(model_dir, exist_ok=True)
            save_json(
                os.path.join(
                    model_dir,
                    TRAIN_SET_METADATA_FILE_NAME
                ),
                train_set_metadata
            )

    # run the experiment
    model, result = train(
        training_set=training_set,
        validation_set=validation_set,
        test_set=test_set,
        model_definition=model_definition,
        save_path=model_dir,
        model_load_path=model_load_path,
        resume=model_resume_path is not None,
        skip_save_model=skip_save_model,
        skip_save_progress=skip_save_progress,
        skip_save_log=skip_save_log,
        gpus=gpus,
        gpu_fraction=gpu_fraction,
        use_horovod=use_horovod,
        random_seed=random_seed,
        debug=debug
    )

    train_trainset_stats, train_valisest_stats, train_testset_stats = result
    train_stats = {
        'train': train_trainset_stats,
        'validation': train_valisest_stats,
        'test': train_testset_stats
    }

    if should_close_session:
        model.close_session()

    if is_on_master():
        # save training and test statistics
        save_json(training_stats_fn, train_stats)

    # grab the results of the model with highest validation test performance
    validation_field = model_definition['training']['validation_field']
    validation_measure = model_definition['training']['validation_measure']
    validation_field_result = train_valisest_stats[validation_field]

    best_function = get_best_function(validation_measure)
    # results of the model with highest validation test performance
    if is_on_master() and validation_set is not None:
        epoch_best_vali_measure, best_vali_measure = best_function(
            enumerate(validation_field_result[validation_measure]),
            key=lambda pair: pair[1]
        )
        logger.info(
            'Best validation model epoch: {0}'.format(
                epoch_best_vali_measure + 1)
        )
        logger.info(
            'Best validation model {0} on validation set {1}: {2}'.format(
                validation_measure, validation_field, best_vali_measure
            ))
        if test_set is not None:
            best_vali_measure_epoch_test_measure = train_testset_stats[
                validation_field][validation_measure][epoch_best_vali_measure]

            logger.info(
                'Best validation model {0} on test set {1}: {2}'.format(
                    validation_measure,
                    validation_field,
                    best_vali_measure_epoch_test_measure
                )
            )
        logger.info('\nFinished: {0}_{1}'.format(experiment_name, model_name))
        logger.info('Saved to: {0}'.format(experiment_dir_name))

    contrib_command("train_save", experiment_dir_name)

    return (model,
            (
                training_set,
                validation_set,
                test_set,
                train_set_metadata
            ),
            experiment_dir_name,
            train_stats,
            model_definition
            )


def cli(sys_argv):
    parser = argparse.ArgumentParser(
        description='This script trains a model using preprocessed data stored '
                    'as parquet files.',
        prog='ludwig train_preprocessed',
        usage='%(prog)s [options]'
    )

    # ----------------------------
    # Experiment naming parameters
    # ----------------------------
    parser.add_argument(
        '--output_directory',
        type=str,
        default='results',
        help='directory that contains the results'
    )
    parser.add_argument(
        '--experiment_name',
        type=str,
        default='experiment',
        help='experiment name'
    )
    parser.add_argument(
        '--model_name',
        type=str,
        default='run',
        help='name for the model'
    )

    # ---------------
    # Data parameters
    # ---------------
    parser.add_argument('--train_parquet_path', help='input train data CSV file')
    parser.add_argument(
        '--validation_parquet_path',
        help='input validation data CSV file'
    )
    parser.add_argument('--test_parquet_path', help='input test data CSV file')
    parser.add_argument(
        '--train_set_metadata_json',
        help='input metadata JSON file. It is an intermediate preprocess file '
             'containing the mappings of the input CSV created the first time a'
             ' CSV file is used in the same directory with the same name and a '
             'json extension'
    )

    # ----------------
    # Model parameters
    # ----------------
    model_definition = parser.add_mutually_exclusive_group(required=True)
    model_definition.add_argument(
        '-md',
        '--model_definition',
        type=yaml.safe_load,
        help='model definition'
    )
    model_definition.add_argument(
        '-mdf',
        '--model_definition_file',
        help='YAML file describing the model. Ignores --model_hyperparameters'
    )

    parser.add_argument(
        '-mlp',
        '--model_load_path',
        help='path of a pretrained model to load as initialization'
    )
    parser.add_argument(
        '-mrp',
        '--model_resume_path',
        help='path of a the model directory to resume training of'
    )
    parser.add_argument(
        '-sstd',
        '--skip_save_training_description',
        action='store_true',
        default=False,
        help='disables saving the description JSON file'
    )
    parser.add_argument(
        '-ssts',
        '--skip_save_training_statistics',
        action='store_true',
        default=False,
        help='disables saving training statistics JSON file'
    )
    parser.add_argument(
        '-ssm',
        '--skip_save_model',
        action='store_true',
        default=False,
        help='disables saving weights each time the model imrpoves. '
             'By default Ludwig saves  weights after each epoch '
             'the validation measure imrpvoes, but  if the model is really big '
             'that can be time consuming if you do not want to keep '
             'the weights and just find out what performance can a model get '
             'with a set of hyperparameters, use this parameter to skip it.'
    )
    parser.add_argument(
        '-ssp',
        '--skip_save_progress',
        action='store_true',
        default=False,
        help='disables saving weights after each epoch. By default ludwig saves '
             'weights after each epoch for enabling resuming of training, but '
             'if the model is really big that can be time consuming and will '
             'save twice as much space, use this parameter to skip it.'
    )
    parser.add_argument(
        '-ssl',
        '--skip_save_log',
        action='store_true',
        default=False,
        help='disables saving TensorBoard logs. By default Ludwig saves '
             'logs for the TensorBoard, but if it is not needed turning it off '
             'can slightly increase the overall speed.'
    )

    # ------------------
    # Runtime parameters
    # ------------------
    parser.add_argument(
        '-rs',
        '--random_seed',
        type=int,
        default=42,
        help='a random seed that is going to be used anywhere there is a call '
             'to a random number generator: data splitting, parameter '
             'initialization and training set shuffling'
    )
    parser.add_argument(
        '-g',
        '--gpus',
        nargs='+',
        type=int,
        default=None,
        help='list of gpus to use'
    )
    parser.add_argument(
        '-gf',
        '--gpu_fraction',
        type=float,
        default=1.0,
        help='fraction of gpu memory to initialize the process with'
    )
    parser.add_argument(
        '-uh',
        '--use_horovod',
        action='store_true',
        default=False,
        help='uses horovod for distributed training'
    )
    parser.add_argument(
        '-dbg',
        '--debug',
        action='store_true',
        default=False, help='enables debugging mode'
    )
    parser.add_argument(
        '-l',
        '--logging_level',
        default='info',
        help='the level of logging to use',
        choices=['critical', 'error', 'warning', 'info', 'debug', 'notset']
    )

    args = parser.parse_args(sys_argv)

    logging.getLogger('ludwig').setLevel(
        logging_level_registry[args.logging_level]
    )
    set_on_master(args.use_horovod)

    if is_on_master():
        print_ludwig('Train Preprocessed', LUDWIG_VERSION)

    train_preprocessed(**vars(args))


if __name__ == '__main__':
    contrib_command("train_preprocessed", *sys.argv)
    cli(sys.argv[1:])