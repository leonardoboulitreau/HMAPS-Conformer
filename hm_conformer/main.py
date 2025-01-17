import os
import sys
import random
import datetime
import numpy as np
import pandas as pd
import torch
from torch.utils.data import DataLoader, DistributedSampler

if os.path.exists('/exp_lib'):
    sys.path.append('/exp_lib')
import egg_exp
import arguments
import data_processing
import train

def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    
def run(process_id, args, experiment_args):
    #===================================================
    #                    Setting      
    #===================================================
    torch.cuda.empty_cache()
    
    # set reproducible
    set_seed(args['rand_seed'])
    
    # DDP 
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = args['port']
    args['rank'] = process_id
    args['device'] = f'cuda:{process_id}'
    torch.distributed.init_process_group(
            backend='nccl', world_size=args['world_size'], rank=args['rank'])
    flag_parent = process_id == 0

    # logger
    if flag_parent:
        builder = egg_exp.log.LoggerList.Builder(args['name'], args['project'], args['tags'], args['description'], args['path_scripts'], args)
        builder.use_local_logger(args['path_log'])
        # builder.use_neptune_logger(args['neptune_user'], args['neptune_token'])
        # builder.use_wandb_logger(args['wandb_entity'], args['wandb_api_key'], args['wandb_group'])
        logger = builder.build()
        logger.log_arguments(experiment_args)
    else:
        logger = None
    
    # data loader
    print("> Creating Dataset")
    asvspoof = egg_exp.data.dataset.ASVspoof2021_DF_LA(args['path_train'], args['path_test'], args['path_test_LA'], DA_speed=args['DA_codec_speed'], print_info=flag_parent, path_eval_2024=args['path_eval2024'], path_dev_2024=args['path_dev2024'])
    print("> Created Dataset")

    train_set = data_processing.TrainSet(asvspoof.train_set_2024, args['train_crop_size'], args['DA_p'], args['DA_list'], args['DA_params'])
    train_sampler = DistributedSampler(train_set, shuffle=True)
    train_loader = DataLoader(
        train_set,
        num_workers=args['num_workers'],
        batch_size=args['batch_size'],
        pin_memory=True,
        sampler=train_sampler,
        drop_last=True
    )

    traintest_set = data_processing.TestSet(asvspoof.traintest_set_2024, args['train_crop_size'])
    traintest_sampler = DistributedSampler(traintest_set, shuffle=False)
    traintest_loader = DataLoader(
        traintest_set,
        num_workers=args['num_workers'],
        batch_size=args['batch_size'],
        pin_memory=True,
        sampler=traintest_sampler,
        drop_last=False
    )

    dev2024_set = data_processing.TestSet(asvspoof.dev_set_2024, args['val_crop_size'])
    dev2024_sampler = DistributedSampler(dev2024_set, shuffle=False)
    dev2024_loader = DataLoader(
        dev2024_set,
        num_workers=args['num_workers'],
        batch_size=args['batch_size'],
        pin_memory=True,
        sampler=dev2024_sampler,
        drop_last=False
    )
    
    # Waveform augmentation
    augmentation = None
    if len(args['DA_wav_aug_list']) != 0:
        augmentation = egg_exp.data.augmentation.WaveformAugmetation(args['DA_wav_aug_list'], args['DA_wav_aug_params'])
    
    # data preprocessing
    preprocessing = egg_exp.framework.model.LFCC(args['sample_rate'], args['n_lfcc'], 
            args['coef'], args['n_fft'], args['win_length'], args['hop'], args['with_delta'], args['with_emphasis'], args['with_energy'],
            args['DA_frq_mask'], args['DA_frq_p'], args['DA_frq_mask_max'])

    #preprocessing = egg_exp.framework.model.MelSpectrogram(sample_rate = args['sample_rate'], coef = args['coef'], 
    #        n_fft = args['n_fft'], win_length = args['win_length'], hop=args['hop'], with_delta=args['with_delta'], with_emphasis=args['with_emphasis'], in_db=False, frq_mask=args['DA_frq_mask'], 
    #        p=args['DA_frq_p'], max=args['DA_frq_mask_max'])

    # frontend
    frontend = egg_exp.framework.model.HM_Conformer(bin_size=args['bin_size'], output_size=args['output_size'], input_layer=args['input_layer'],
            pos_enc_layer_type=args['pos_enc_layer_type'], linear_units=args['linear_units'], cnn_module_kernel=args['cnn_module_kernel'],
            dropout=args['dropout'], emb_dropout=args['emb_dropout'], multiloss=True)

    # backend
    backends = []
    criterions = []
    for i in range(5):
        backend = egg_exp.framework.model.CLSBackend(in_dim=args['output_size'], hidden_dim=args['embedding_size'], use_pooling=args['use_pooling'], input_mean_std=args['input_mean_std'])
        backends.append(backend)
        
        # criterion
        criterion = egg_exp.framework.loss.OCSoftmax(embedding_size=args['embedding_size'], 
            num_class=args['num_class'], feat_dim=args['feat_dim'], r_real=args['r_real'], 
            r_fake=args['r_fake'], alpha=args['alpha'])
        criterions.append(criterion)
    
    # set framework
    if augmentation != None:
        framework = egg_exp.framework.DeepfakeDetectionFramework_DA_multiloss(
            augmentation=augmentation,
            preprocessing=preprocessing,
            frontend=frontend,
            backend=backends,
            loss=criterions,
            loss_weight=args['loss_weight'],
        )
    else:
        framework = egg_exp.framework.DeepfakeDetectionFramework(
            preprocessing=preprocessing,
            frontend=frontend,
            backend=backend,
            loss=criterion,
        )
    framework.use_distributed_data_parallel(f'cuda:{process_id}', True)

    # optimizer
    optimizer = torch.optim.Adam(framework.get_parameters(), lr=args['lr'], weight_decay=args['weight_decay'])
        
    # lr scheduler
    scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
        optimizer,
        T_0=args['epoch'],
        T_mult=args['T_mult'],
        eta_min=args['lr_min']
    )

    # ===================================================
    #                    Test
    # ===================================================

    if args['TEST']:

        ################## PROGRESS SET 2024 ##################

        eval2024_set = data_processing.TestSet(asvspoof.eval_set_2024, args['test_crop_size'])
        print('Eval Crop Size = ', args['test_crop_size'])
        eval2024_loader = DataLoader(
            eval2024_set,
            num_workers=args['num_workers'],
            batch_size=1,
            pin_memory=True,
            drop_last=False
        )

        # Load Model
        framework.load_model(args)

        print('Running Test on 2024 eval')
        eer_repo, scores, labels, filenames, eer, dcf, cllr = train.test(framework, eval2024_loader, run_on_ddp=False)

        df = pd.DataFrame([filenames, scores]).T
        df.rename(columns={0:'filename', 1:'cm-score'}, inplace=True)
        df['cm-score'] = df['cm-score'].apply(lambda x: float(x))
        df.to_csv(path_or_buf=os.path.join(args['path_log'], args['project']) + '/score.tsv', sep = '\t', index=False)

        ################## DEV SET 2024 ##################

        # Load Model
        framework.load_model(args)

        print('Running Test on 2024 dev')
        eer_repo, scores, labels, filenames, eer, dcf, cllr = train.test(framework, dev2024_loader, run_on_ddp=True)
        print(eer_repo, eer, dcf, cllr)    
        
        ################## DF SET 2021 ##################

        df2021_set = data_processing.TestSet(asvspoof.dev_set_2024, args['val_crop_size'])
        df2021_sampler = DistributedSampler(df2021_set, shuffle=False)
        dev2024_loader = DataLoader(
            df2021_set,
            num_workers=args['num_workers'],
            batch_size=args['batch_size'],
            pin_memory=True,
            sampler=df2021_sampler,
            drop_last=False
        )

        # Load Model
        framework.load_model(args)

        eer_repo, scores, labels, filenames, eer, dcf, cllr = train.test(framework, dev2024_loader, run_on_ddp=True)
        print(eer_repo, eer, dcf, cllr)    

    # ===================================================
    #                    Train
    # ===================================================
    else:
        best_eer_DF = 100
        best_dcf = 100
        best_state_DF = framework.copy_state_dict()
        cnt_early_stop = 0

        # load model
        pre_trained_model = os.path.join(args['path_scripts'], 'model')
        if os.path.exists(pre_trained_model):
            state_dict = {}
            for pt in os.listdir((pre_trained_model)):
                state_dict[pt.replace('.pt', '')] = torch.load(pt)
            framework.load_state_dict(state_dict)
            print("Loading Model...")
        else:
            print("Training from Scratch")

        for epoch in range(1, args['epoch'] + 1):

            scheduler.step(epoch)

            # train
            train_sampler.set_epoch(epoch)
            train.train(epoch, framework, optimizer, train_loader, logger)

            if logger is not None:
                logger.log_metric('LR', scheduler.get_last_lr(), epoch)

            # Tests

            # Test on Train
            #if epoch % 1 == 0:
            #    eer_repo, scores, labels, filenames, eer, dcf, cllr = train.test(framework, traintest_loader, run_on_ddp=True)
            #    if logger is not None:
            #        logger.log_metric('Train-EER-repo', eer_repo, epoch)
            #        logger.log_metric('Train-EER', eer, epoch)
            #        logger.log_metric('Train-DCF', dcf, epoch)
            #        logger.log_metric('Train-CLLR', cllr, epoch)

            # Test on Dev
            if epoch % 1 == 0:
                cnt_early_stop += 1
                eer_repo, scores, labels, filenames, eer, dcf, cllr = train.test(framework, dev2024_loader, run_on_ddp=True)
                
                if logger is not None:
                    logger.log_metric('Dev-EER-repo', eer_repo, epoch)
                    logger.log_metric('Dev-EER', eer, epoch)
                    logger.log_metric('Dev-DCF', dcf, epoch)
                    logger.log_metric('Dev-CLLR', cllr, epoch)

                # logging
                if eer_repo < best_eer_DF:
                    cnt_early_stop = 0
                    best_eer_DF = eer_repo
                    best_state_ft = framework.copy_state_dict()
                    if logger is not None:
                        logger.log_metric('BestEER', eer_repo, epoch)
                        for key, v in best_state_ft.items():
                            logger.save_model(
                                f'check_point_DF_{key}_{epoch}', v)

                if dcf < best_dcf:
                    cnt_early_stop = 0
                    best_dcf = dcf
                    best_state_ft = framework.copy_state_dict()
                    if logger is not None:
                        logger.log_metric('BestDCF', dcf, epoch)
                        for key, v in best_state_ft.items():
                            logger.save_model(
                                f'check_point_DF_{key}_{epoch}', v)

                if cnt_early_stop >= 30:
                    break
                

if __name__ == '__main__':
    # get arguments
    args, system_args, experiment_args = arguments.get_args()
    
    # set reproducible
    set_seed(args['rand_seed'])

    # check gpu environment
    if args['usable_gpu'] is None: 
        args['gpu_ids'] = os.environ['CUDA_VISIBLE_DEVICES'].split(',')
    else:
        os.environ['CUDA_VISIBLE_DEVICES'] = args['usable_gpu']
        args['gpu_ids'] = args['usable_gpu'].split(',')
    assert 0 < len(args['gpu_ids']), 'Only GPU env are supported'
    
    args['port'] = f'10{datetime.datetime.now().microsecond % 100}'

    # set DDP
    args['world_size'] = len(args['gpu_ids'])
    args['batch_size'] = args['batch_size'] // args['world_size']
    if args['batch_size'] % args['world_size'] != 0:
        print(f'The batch size is resized to {args["batch_size"] * args["world_size"]} because the rest are discarded.')
    torch.cuda.empty_cache()
    
    # start
    torch.multiprocessing.set_sharing_strategy('file_system')
    torch.multiprocessing.spawn(
        run, 
        nprocs=args['world_size'], 
        args=(args, experiment_args)
    )
