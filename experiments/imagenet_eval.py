"""Run cifar100 experiments for the paper.
"""
import time, os, math
from multiprocessing import Pool

import models.params
from datasets import cifar, mnist, svhn
import models
from models.train import train
from models.evaluate import evaluate
from models import pixeldp_cnn, pixeldp_resnet, madry
import attacks
from attacks import pgd, params, train_attack, evaluate_attack
import plots.plot_robust_accuracy
import plots.plot_accuracy_under_attack

import tensorflow as tf

import numpy as np

from flags import FLAGS


def train_eval_model(args):
    model_name = args[0]
    model = models.module_from_name(model_name)
    param_dict = args[1]
    current_gpu = args[2]

    dir_name = os.path.join(FLAGS.models_dir, 'imagenet')
    compute_robustness = False
    if param_dict['attack_norm_bound'] == 0.0:
        param_dict['batch_size'] = 100
        param_dict['n_draws']    = 1
    else:
        param_dict['batch_size'] = 1
        param_dict['n_draws']    = 100

    hps   = models.params.HParams(**param_dict)
    with tf.Graph().as_default():
        evaluate(hps, model, dataset='imagenet', dir_name=dir_name,
                compute_robustness=compute_robustness,
                dev='/gpu:{}'.format(current_gpu))

    return hps, model_name


def train_eval_attack(args):
    hps = args[0]
    model_name = args[1]
    model = models.module_from_name(model_name)
    attack_param_dict = args[2]
    current_gpu = args[3]
    dir_name = os.path.join(FLAGS.models_dir, 'imagenet')

    attack_params = attacks.params.AttackParams(**attack_param_dict)
    atk = attacks.module_from_name(attack_params.attack_methodolody)

    with tf.Graph().as_default():
        train_attack.train_one(
                'imagenet', model, hps, atk, attack_params, dir_name=dir_name,
                dev='/gpu:{}'.format(current_gpu)
        )

    with tf.Graph().as_default():
        evaluate_attack.evaluate_one(
                'imagenet', model, hps, atk, attack_params, dir_name=dir_name,
                dev='/gpu:{}'.format(current_gpu)
        )

    return hps, attack_params, model_name


def run(plots_only=False):
    _param_dict = {
        'name_prefix': '',
        'steps_num': 70000,
        'eval_data_size': 1000,
        'image_size': 299,
        'n_channels': 3,
        'num_classes': 1001,
        'relu_leakiness': 0.1,
        'lrn_rate': 0.1,
        'lrn_rte_changes': [40000, 50000, 60000],
        'lrn_rte_vals': [0.01, 0.001, 0.0001],
        'num_residual_units': 4,
        'use_bottleneck': False,
        'weight_decay_rate': 0.0002,
        'optimizer': 'mom',
        'image_standardization': False,
        'dp_epsilon': 1.0,
        'dp_delta': 0.05,
        'robustness_confidence_proba': 0.05,
        'attack_norm': 'l2',
        'sensitivity_norm': 'l2',
        'sensitivity_control_scheme': 'bound',  # bound or optimize
        'layer_sensitivity_bounds': ['l2_l2'],
        'noise_after_activation': True,
        'parseval_loops': 10,
        'parseval_step': 0.0003,
    }

    params = []
    parallelizable_arguments_list = []
    num_gpus = max(1, FLAGS.num_gpus)

    Ls = [0., 0.1, 0.3, 1.0]

    # First, create all params for train/eval models.
    for model_name in ["inception-v3"]:
        for attack_norm_bound in Ls:
            for noise_after_n_layers in [-1, 1]:
                # Add only one experiment for Madry
                if model_name == "madry":
                    if attack_norm_bound != 0.0 or noise_after_n_layers != -1:
                        continue
                if attack_norm_bound == 0.0 and noise_after_n_layers > -1:
                    continue  # The baseline can only have -1.
                if attack_norm_bound > 0.0 and noise_after_n_layers < 0:
                    continue  # PixelDP nets need a noise layer at position >= 0.
                param_dict = dict(_param_dict)
                if attack_norm_bound == 0.0:
                    param_dict['parseval_loops'] = 0
                else:
                    param_dict['parseval_loops'] = math.ceil(100 * attack_norm_bound)

                param_dict['attack_norm_bound']    = attack_norm_bound
                param_dict['noise_after_n_layers'] = noise_after_n_layers
                if not plots_only:
                    parallelizable_arguments_list.append(
                        (
                            model_name,
                            dict(param_dict),
                            len(parallelizable_arguments_list) % num_gpus
                        )
                    )
                else:
                    param_dict = dict(param_dict)
                    param_dict['batch_size'] = 1
                    param_dict['n_draws']    = 1
                    hps = models.params.HParams(**param_dict)
                    parallelizable_arguments_list.append((hps, model_name))

    # Run train/eval of models.
    if not plots_only:
        print("Train/Eval models:: Total experiments: {}".\
              format(len(parallelizable_arguments_list)))
        print("Train/Eval models:: Running on {} GPUs\n\n".format(num_gpus))
        results = []
        for i in range(0, len(parallelizable_arguments_list), num_gpus):
            train_eval_model(parallelizable_arguments_list[i])
            p = Pool(processes=num_gpus)
            current = p.map(train_eval_model, parallelizable_arguments_list[i:min(i+num_gpus,len(parallelizable_arguments_list))])
            results.extend(current)
            p.close()
            p.join()
            time.sleep(5)
    else:
        results = parallelizable_arguments_list

    # Second, create all params for train/eval attacks on models.
    parallelizable_arguments_list = []

    _attack_param_dict = {
        'restarts': 10,
        'n_draws_attack': 20,
        'n_draws_eval':   300,
        'attack_norm': 'l2',
        'max_attack_size': -1,
        'num_examples': 1000,
        'attack_methodolody': 'pgd',
        'targeted': False,
        'sgd_iterations': 100,
        'use_softmax': False,
    }

    use_attack_methodology = 'carlini'
    pgd_sizes = [round(x, 2) for x in np.arange(0.1, 1.5, 0.1).tolist()]

    for (hps, model_name) in results:
        attack_param_dict = dict(_attack_param_dict)
        if use_attack_methodology == 'pgd':
            attack_param_dict['attack_methodolody'] = "pgd"
            attack_param_dict['n_draws_attack'] = 10
            attack_param_dict['n_draws_eval'] = 500
            attack_param_dict['restarts'] = 10
            for attack_size in pgd_sizes:
                attack_size = round(attack_size, 2)
                attack_param_dict['max_attack_size'] = max_attack_size
                if not plots_only:
                    parallelizable_arguments_list.append(
                        (
                            hps,
                            model_name,
                            dict(attack_param_dict),
                            len(parallelizable_arguments_list) % num_gpus
                        )
                    )
                else:
                    attack_params = attacks.params.AttackParams(**attack_param_dict)
                    parallelizable_arguments_list.append((
                        hps, attack_params, model_name
                    ))

        if use_attack_methodology == 'carlini':
            attack_param_dict['attack_methodolody'] = "carlini"
            attack_param_dict['max_attack_size'] = 5
            attack_param_dict['restarts'] = 1
            if hps.attack_norm_bound <= 0:
                # Baseline, onlie argmax
                use_softmax_vals = [False]
                attack_param_dict['n_draws_eval'] = 1
                attack_param_dict['n_draws_attack'] = 1
            else:
                # pixeldp try both
                use_softmax_vals = [True, False]
                use_softmax_vals = [True]
                attack_param_dict['n_draws_attack'] = 20
                attack_param_dict['n_draws_eval'] = 300

            for use_softmax in use_softmax_vals:
                attack_param_dict['use_softmax'] = use_softmax
                if not plots_only:
                    parallelizable_arguments_list.append(
                            (
                                hps,
                                model_name,
                                dict(attack_param_dict),
                                len(parallelizable_arguments_list) % num_gpus
                            )
                    )
                else:
                    attack_params = attacks.params.AttackParams(**attack_param_dict)
                    parallelizable_arguments_list.append((
                        hps, attack_params, model_name
                    ))

    # Run train/eval of attracks on models.
    if not plots_only:
        print("\nTrain/Eval attacks:: Experiments: {}".\
              format(parallelizable_arguments_list))
        print("Train/Eval attacks:: Total experiments: {}".\
              format(len(parallelizable_arguments_list)))
        results = []
        for i in range(0, len(parallelizable_arguments_list), num_gpus):
            p = Pool(processes=num_gpus)
            current = p.map(train_eval_attack, parallelizable_arguments_list[i:min(i+num_gpus,len(parallelizable_arguments_list))])
            results.extend(current)
            p.close()
            p.join()
            print("Finished experiments: {}/{}".\
                  format(len(results), len(parallelizable_arguments_list)))
            time.sleep(5)
    else:
        results = parallelizable_arguments_list

    # Retrieve all results after evaluating the attacks and order them in a way
    # that makes ploting scripts happy!
    _robust_model_names = set()
    _robust_models = []
    _robust_params = []
    _models = []
    _params = []
    nonbaseline_attack_params = []
    baseline_attack_params = []
    for (hps, attack_params, model_name) in results:
        if hps.attack_norm_bound == 0.0:
            baseline_model = models.module_from_name(model_name)
            baseline_params = hps
            baseline_attack_params.append(attack_params)
        else:
            model_module = models.module_from_name(model_name)
            _name = models.params.name_from_params(model_module, hps)
            if _name not in _robust_model_names:
                _robust_model_names.add(_name)
                _robust_models.append(model_module)
                _robust_params.append(hps)
            if hps.attack_norm_bound not \
                in list(map(lambda x: x.attack_norm_bound, params)):
                _models.append(models.module_from_name(model_name))
                _params.append(hps)
                nonbaseline_attack_params.append([])
            nonbaseline_attack_params[-1].append(attack_params)

    dir_name = os.path.join(FLAGS.models_dir, 'imagenet')
    # Plot accuracy under attack
    _models = [baseline_model] + _models
    _params = [baseline_params] + _params
    _attack_params = [baseline_attack_params] + nonbaseline_attack_params
    x_ticks = [round(x, 2) for x in np.arange(0.0, 4.1, 0.25).tolist()]
    x_ticks = [round(x, 2) for x in np.arange(0.0, 4.1, 0.1).tolist()]
    x_ticks = x_ticks[1:]
    plots.plot_accuracy_under_attack.plot("imagenet_accuracy_under_attack_argmax",
                                          _models,
                                          _params,
                                          _attack_params,
                                          x_range=(0, 4.0),
                                          x_ticks=x_ticks,
                                          dir_name=dir_name)
    plots.plot_accuracy_under_attack.plot("imagenet_accuracy_under_attack_softmax",
                                          _models,
                                          _params,
                                          _attack_params,
                                          x_range=(0, 4.0),
                                          x_ticks=x_ticks,
                                          dir_name=dir_name,
                                          expectation_layer='softmax')

def main(_):
    run()

