"""
Regression experiment using MAML
"""
import copy
import os
import time
import warnings

import numpy as np
import scipy.stats as st
import torch
import torch.nn.functional as F
import torch.optim as optim

from regression import tasks_sine, utils, tasks_celebA
from regression.default_configs import get_default_config_maml
from regression.logger import Logger
from regression import MamlModel

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


def run(config, log_interval=5000, rerun=False):
    assert config['method'] == 'maml'

    # see if we already ran this experiment
    code_root = os.path.dirname(os.path.realpath(__file__))
    if not os.path.isdir('{}/{}_result_files/'.format(code_root, config['task'])):
        os.mkdir('{}/{}_result_files/'.format(code_root, config['task']))
    path = '{}/{}_result_files/'.format(code_root, config['task']) + utils.get_path_from_config(config)

    if os.path.exists(path + '.pkl') and not rerun:
        return utils.load_obj(path)

    start_time = time.time()

    # warn if there's more keys in the configs than should be
    if not len(config.keys()) == len(get_default_config_maml(config['task']).keys()):
        warnings.warn('It seems like additional keys were added to the default config.')
        print([k for k in config.keys() if k not in get_default_config_maml(config['task']).keys()])

    # correctly seed everything
    utils.set_seed(config['seed'])

    # --- initialise everything ---

    # get the task family
    if config['task'] == 'sine':
        task_family_train = tasks_sine.RegressionTasksSinusoidal()
        task_family_valid = tasks_sine.RegressionTasksSinusoidal()
        task_family_test = tasks_sine.RegressionTasksSinusoidal()
    elif config['task'] == 'celeba':
        task_family_train = tasks_celebA.CelebADataset('train')
        task_family_valid = tasks_celebA.CelebADataset('valid')
        task_family_test = tasks_celebA.CelebADataset('test')
    else:
        raise NotImplementedError

    # initialise network
    model_inner = MamlModel(task_family_train.num_inputs,
                            task_family_train.num_outputs,
                            n_weights=config['n_hidden'],
                            num_context_params=config['num_context_params'],
                            ).to(device)
    model_outer = copy.deepcopy(model_inner)

    # intitialise meta-optimiser
    meta_optimiser = optim.Adam(model_outer.weights + model_outer.biases + [model_outer.task_context],
                                config['lr_meta'])

    # initialise loggers
    logger = Logger()
    logger.best_valid_model = copy.deepcopy(model_outer)

    for i_iter in range(config['n_iter']):

        # copy weights of network
        copy_weights = [w.clone() for w in model_outer.weights]
        copy_biases = [b.clone() for b in model_outer.biases]
        copy_context = model_outer.task_context.clone()

        # get all shared parameters and initialise cumulative gradient
        meta_gradient = [0 for _ in range(len(copy_weights + copy_biases) + 1)]

        # sample tasks
        target_functions = task_family_train.sample_tasks(config['tasks_per_metaupdate'])

        for t in range(config['tasks_per_metaupdate']):

            # reset network weights
            model_inner.weights = [w.clone() for w in copy_weights]
            model_inner.biases = [b.clone() for b in copy_biases]
            model_inner.task_context = copy_context.clone()

            # get data for current task
            train_inputs = task_family_train.sample_inputs(config['k_meta_train'], config['order_pixels'])

            for _ in range(config['num_inner_updates']):

                # forward through network
                outputs = model_outer(train_inputs)

                # get targets
                targets = target_functions[t](train_inputs)

                # ------------ update on current task ------------

                # compute loss for current task
                loss_task = F.mse_loss(outputs, targets)

                # update private parts of network and keep correct computation graph
                params = [w for w in model_outer.weights] + [b for b in model_outer.biases] + [model_outer.task_context]
                grads = torch.autograd.grad(loss_task, params, create_graph=True, retain_graph=True)
                for i in range(len(model_inner.weights)):
                    if not config['first_order']:
                        model_inner.weights[i] = model_outer.weights[i] - config['lr_inner'] * grads[i]
                    else:
                        model_inner.weights[i] = model_outer.weights[i] - config['lr_inner'] * grads[i].detach()
                for j in range(len(model_inner.biases)):
                    if not config['first_order']:
                        model_inner.biases[j] = model_outer.biases[j] - config['lr_inner'] * grads[i + j + 1]
                    else:
                        model_inner.biases[j] = model_outer.biases[j] - config['lr_inner'] * grads[i + j + 1].detach()
                if not config['first_order']:
                    model_inner.task_context = model_outer.task_context - config['lr_inner'] * grads[i + j + 2]
                else:
                    model_inner.task_context = model_outer.task_context - config['lr_inner'] * grads[i + j + 2].detach()

            # ------------ compute meta-gradient on test loss of current task ------------

            # get test data
            test_inputs = task_family_train.sample_inputs(config['k_meta_test'], config['order_pixels'])

            # get outputs after update
            test_outputs = model_inner(test_inputs)

            # get the correct targets
            test_targets = target_functions[t](test_inputs)

            # compute loss (will backprop through inner loop)
            loss_meta = F.mse_loss(test_outputs, test_targets)

            # compute gradient w.r.t. *outer model*
            task_grads = torch.autograd.grad(loss_meta,
                                             model_outer.weights + model_outer.biases + [model_outer.task_context])
            for i in range(len(model_inner.weights + model_inner.biases) + 1):
                meta_gradient[i] += task_grads[i].detach()

        # ------------ meta update ------------

        meta_optimiser.zero_grad()
        # print(meta_gradient)

        # assign meta-gradient
        for i in range(len(model_outer.weights)):
            model_outer.weights[i].grad = meta_gradient[i] / config['tasks_per_metaupdate']
            meta_gradient[i] = 0
        for j in range(len(model_outer.biases)):
            model_outer.biases[j].grad = meta_gradient[i + j + 1] / config['tasks_per_metaupdate']
            meta_gradient[i + j + 1] = 0
        model_outer.task_context.grad = meta_gradient[i + j + 2] / config['tasks_per_metaupdate']
        meta_gradient[i + j + 2] = 0

        # do update step on outer model
        meta_optimiser.step()

        # ------------ logging ------------

        if i_iter % log_interval == 0:

            # evaluate on training set
            loss_mean, loss_conf = eval(config, copy.deepcopy(model_outer), task_family=task_family_train,
                                        num_updates=config['num_inner_updates'])
            logger.train_loss.append(loss_mean)
            logger.train_conf.append(loss_conf)

            # evaluate on test set
            loss_mean, loss_conf = eval(config, copy.deepcopy(model_outer), task_family=task_family_valid,
                                        num_updates=config['num_inner_updates'])
            logger.valid_loss.append(loss_mean)
            logger.valid_conf.append(loss_conf)

            # evaluate on validation set
            loss_mean, loss_conf = eval(config, copy.deepcopy(model_outer), task_family=task_family_test,
                                        num_updates=config['num_inner_updates'])
            logger.test_loss.append(loss_mean)
            logger.test_conf.append(loss_conf)

            # save logging results
            utils.save_obj(logger, path)

            # save best model
            if logger.valid_loss[-1] == np.min(logger.valid_loss):
                print('saving best model at iter', i_iter)
                logger.best_valid_model = copy.deepcopy(model_outer)

            # visualise results
            if config['task'] == 'celeba':
                tasks_celebA.visualise(task_family_train, task_family_test, copy.deepcopy(logger.best_valid_model),
                                       config, i_iter)

            # print current results
            logger.print_info(i_iter, start_time)
            start_time = time.time()

    return logger


def eval(config, model, task_family, num_updates, n_tasks=100, return_gradnorm=False):
    # copy weights of network
    copy_weights = [w.clone() for w in model.weights]
    copy_biases = [b.clone() for b in model.biases]
    copy_context = model.task_context.clone()

    # get the task family (with infinite number of tasks)
    input_range = task_family.get_input_range()

    # logging
    losses = []
    gradnorms = []

    # --- inner loop ---

    for t in range(n_tasks):

        # reset network weights
        model.weights = [w.clone() for w in copy_weights]
        model.biases = [b.clone() for b in copy_biases]
        model.task_context = copy_context.clone()

        # sample a task
        target_function = task_family.sample_task()

        # get data for current task
        curr_inputs = task_family.sample_inputs(config['k_shot_eval'], config['order_pixels'])
        curr_targets = target_function(curr_inputs)

        # ------------ update on current task ------------

        for _ in range(1, num_updates + 1):

            curr_outputs = model(curr_inputs)

            # compute loss for current task
            task_loss = F.mse_loss(curr_outputs, curr_targets)

            # update task parameters
            params = [w for w in model.weights] + [b for b in model.biases] + [model.task_context]
            grads = torch.autograd.grad(task_loss, params)

            gradnorms.append(np.mean(np.array([g.norm().item() for g in grads])))

            for i in range(len(model.weights)):
                model.weights[i] = model.weights[i] - config['lr_inner'] * grads[i].detach()
            for j in range(len(model.biases)):
                model.biases[j] = model.biases[j] - config['lr_inner'] * grads[i + j + 1].detach()
            model.task_context = model.task_context - config['lr_inner'] * grads[i + j + 2].detach()

        # ------------ logging ------------

        # compute true loss on entire input range
        losses.append(F.mse_loss(model(input_range), target_function(input_range)).detach().item())

    # reset network weights
    model.weights = [w.clone() for w in copy_weights]
    model.biases = [b.clone() for b in copy_biases]
    model.task_context = copy_context.clone()

    losses_mean = np.mean(losses)
    losses_conf = st.t.interval(0.95, len(losses) - 1, loc=losses_mean, scale=st.sem(losses))

    if not return_gradnorm:
        return losses_mean, np.mean(np.abs(losses_conf - losses_mean))
    else:
        return losses_mean, np.mean(np.abs(losses_conf - losses_mean)), np.mean(gradnorms)


if __name__ == '__main__':

    config = get_default_config_maml(task='sine')
    # config = get_default_config_maml(task='celeba')

    logger = run(config, log_interval=100, rerun=True)
