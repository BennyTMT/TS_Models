from data_provider.data_factory import data_provider
from exp.exp_basic import Exp_Basic
from utils.tools import EarlyStopping, adjust_learning_rate, visual
from utils.metrics import metric
from utils.distillationLoss import DistillationLoss
import torch
import torch.nn as nn
from torch import optim
import os
import time
import warnings
import numpy as np
import torch.nn.functional as F
from exp.ablUtils import * 
from sklearn.utils import resample

os.environ["CUDA_VISIBLE_DEVICES"] = "0,1,2"
'''

ls /p/selfdrivingpj/projects_time/LLaTA/checkpoints/long_term_forecast_ETTm1_96_336_llm_to_trsf_GPT4TS_ETTm1_ftM_sl96_ll0_pl336_dm768_nh4_el2_dl1_df768_fc1_ebtimeF_dtTrue_test_gpt6_0/checkpoint.pth

'''
warnings.filterwarnings('ignore')

class Exp_Long_Term_Forecast(Exp_Basic):
    def __init__(self, args  ):
        super(Exp_Long_Term_Forecast, self).__init__(args)
        
    def _build_model(self):
        model = self.model_dict[self.args.model].Model(self.args, self.device).float()
        self.is_first = True 
        if self.args.use_multi_gpu and self.args.use_gpu:
            model = nn.DataParallel(model, device_ids=self.args.device_ids)
        return model
        
    def _get_data(self, flag, vali_test=False):
        data_set, data_loader = data_provider(self.args, flag, vali_test)
        return data_set, data_loader

    def _select_optimizer(self):
        param_dict = [
            {"params": [p for n, p in self.model.named_parameters() if p.requires_grad and '_proj' in n], "lr": 1e-4},
            {"params": [p for n, p in self.model.named_parameters() if p.requires_grad and '_proj' not in n], "lr": self.args.learning_rate}
        ]
        model_optim = optim.Adam([param_dict[1]], lr=self.args.learning_rate)
        loss_optim = optim.Adam([param_dict[0]], lr=self.args.learning_rate)

        return model_optim, loss_optim

    def _select_criterion(self):
        criterion = DistillationLoss(self.args.distill_loss, 
                                     self.args.logits_loss, 
                                     self.args.task_loss, 
                                     self.args.task_name, 
                                     self.args.feature_w, 
                                     self.args.logits_w, 
                                     self.args.task_w)
        return criterion
        
    def train(self, setting):
        print( 'gpu——count1: ', torch.cuda.device_count())
        train_data, train_loader = self._get_data(flag='train')
        vali_data, vali_loader = self._get_data(flag='val')
        test_data, test_loader = self._get_data(flag='test', vali_test=True)
        
        path = os.path.join(self.args.checkpoints, setting)
        if not os.path.exists(path):
            os.makedirs(path)
            
        time_now = time.time()
    
        train_steps = len(train_loader)
        early_stopping = EarlyStopping(patience=self.args.patience, verbose=True)
        
        model_optim, loss_optim = self._select_optimizer()
        criterion = self._select_criterion()
        
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(model_optim, T_max=self.args.tmax, eta_min=1e-8)
        print( 'gpu——count2: ', torch.cuda.device_count())
        
        for epoch in range(self.args.train_epochs):
            iter_count = 0
            train_loss = []

            self.model.train()
            epoch_time = time.time()
            # print(epoch, self.device )
            for i, (batch_x, batch_y, batch_x_mark, batch_y_mark) in enumerate(train_loader):
                iter_count += 1
                model_optim.zero_grad()
                loss_optim.zero_grad()
                if self.is_first :
                    print(self.args.data_path , batch_x.shape , batch_y.shape)
                    self.is_first = False
                # batch_x , batch_y  torch.Size([256, 96, 7]) torch.Size([256, 96, 7])
                batch_x = batch_x.float().to(self.device)
                batch_y = batch_y.float().to(self.device)
                outputs_dict = self.model(batch_x)
                # print(outputs_dict["outputs_time"].shape , batch_y.shape)
                # exit()
                loss = criterion(outputs_dict, batch_y)

                train_loss.append(loss.item())

                if (i + 1) % 100 == 0:
                    print("\titers: {0}, epoch: {1} | loss: {2:.7f}".format(i + 1, epoch + 1, loss.item()))
                    speed = (time.time() - time_now) / iter_count
                    left_time = speed * ((self.args.train_epochs - epoch) * train_steps - i)
                    print('\tspeed: {:.4f}s/iter; left time: {:.4f}s'.format(speed, left_time))
                    iter_count = 0
                    time_now = time.time()

                loss.backward()
                model_optim.step()
                loss_optim.step()

            print("Epoch: {} cost time: {}".format(epoch + 1, time.time() - epoch_time))
            train_loss = np.average(train_loss)
            
            vali_loss = self.vali(vali_data, vali_loader, criterion)
            # test_loss = self.vali(test_data, test_loader, criterion)

            # print("Epoch: {0}, Steps: {1} | Train Loss: {2:.7f} Vali Loss: {3:.7f} Test Loss: {4:.7f}".format(
            #     epoch + 1, train_steps, train_loss, vali_loss, test_loss))

            if self.args.cos:
                scheduler.step()
                print("lr = {:.10f}".format(model_optim.param_groups[0]['lr']))
            else:
                adjust_learning_rate(model_optim, epoch + 1, self.args)
                
            early_stopping(vali_loss, self.model, path)
            if early_stopping.early_stop:
                print("Early stopping")
                break
        
        print(torch.cuda.device_count())
        best_model_path = path + '/' + 'checkpoint.pth'
        self.model.load_state_dict(torch.load(best_model_path))

        return self.model

    def vali(self, vali_data, vali_loader, criterion):
        total_loss = []

        self.model.in_layer.eval()
        self.model.out_layer.eval()
        self.model.time_proj.eval()
        self.model.text_proj.eval()

        with torch.no_grad():
            for i, (batch_x, batch_y, batch_x_mark, batch_y_mark) in enumerate(vali_loader):
                batch_x = batch_x.float().to(self.device)
                batch_y = batch_y.float()

                batch_x_mark = batch_x_mark.float().to(self.device)
                batch_y_mark = batch_y_mark.float().to(self.device)

                outputs = self.model(batch_x)
                # TODO 目前只是选择时序模态作为最终最终的输出
                outputs_ensemble = outputs['outputs_time']
                # encoder - decoder
                outputs_ensemble = outputs_ensemble[:, -self.args.pred_len:, :]
                batch_y = batch_y[:, -self.args.pred_len:, :].to(self.device)

                pred = outputs_ensemble.detach().cpu()
                true = batch_y.detach().cpu()
                # TODO 验证集的损失函数并不包含蒸馏损失
                loss = F.mse_loss(pred, true)

                total_loss.append(loss)

        total_loss = np.average(total_loss)

        self.model.in_layer.train()
        self.model.out_layer.train()
        self.model.time_proj.train()
        self.model.text_proj.train()

        return total_loss

    def test(self, setting, test=0 , log_fine_name = '' ,  ii = -1 ):
        test_data, test_loader = self._get_data(flag='test')
        # if test:
        #     print('loading model')
            # self.model.load_state_dict(torch.load(os.path.join('./checkpoints/' + setting, 'checkpoint.pth')))
        preds = []
        trues = []
        self.model.eval()
        with torch.no_grad():
            for i, (batch_x, batch_y, batch_x_mark, batch_y_mark) in enumerate(test_loader):
                batch_x = batch_x.float().to(self.device)
                batch_y = batch_y.float().to(self.device)

                outputs = self.model(batch_x[:, -self.args.seq_len:, :])

                outputs_ensemble = outputs['outputs_time']
                
                outputs_ensemble = outputs_ensemble[:, -self.args.pred_len:, :]
                batch_y = batch_y[:, -self.args.pred_len:, :]

                pred = outputs_ensemble.detach().cpu().numpy()
                true = batch_y.detach().cpu().numpy()

                preds.append(pred)
                trues.append(true)

        preds = np.array(preds)
        trues = np.array(trues)
        print('test shape1:', preds.shape, trues.shape)
        preds = preds.reshape(-1, preds.shape[-2], preds.shape[-1])
        trues = trues.reshape(-1, trues.shape[-2], trues.shape[-1])
        print('test shape2:', preds.shape, trues.shape)
        
        # # result save
        # save_dir_name = log_fine_name.split('.')[0]
        # folder_path = './results/' + save_dir_name + '/'
        # if not os.path.exists(folder_path):
        #     os.makedirs(folder_path)
        
        mae, mse, rmse, mape, mspe = metric(preds, trues)
        print('mse:{}, mae:{}'.format(mse, mae))
        # f = open(folder_path + log_fine_name, 'a')
        # f.write('mse:{}, mae:{}'.format(mse, mae))
        # f.write('\n')
        # f.close()
        
        # np.save(folder_path + f'metrics_{ii}.npy', np.array([mae, mse, rmse, mape, mspe]))
        # np.save(folder_path + f'pred_{ii}.npy', preds)
        # np.save(folder_path + f'true_{ii}.npy', trues)

        return mae, mse
            
    # def bootstrap_preds(self, preds,labels , n_iterations = 1000 ):
    #     '''
    #        preds   (2560, 96, 7) 
    #        labels  (2560, 96, 7)
    #     '''
    #     stats = [] 
    #     res =  np.mean(np.abs(preds - labels), axis=(1, 2))  
    #     for _ in range(n_iterations):
    #         sample = resample(res, n_samples= len(preds) , replace=True )  
    #         stats.append(np.mean(sample) )
    #     return stats

        
    def boot_res(self, preds,labels ):
        n_iterations = 1000
        n_size = len(preds)
        stats = [] 
        # (2560, 96, 7) (2560, 96, 7)
        res =  np.mean(np.abs(preds - labels), axis=(1, 2))  
        print(res.shape)
        assert len(res) == n_size
        
        for _ in range(n_iterations):
            sample = resample(res, n_samples=n_size , replace=True )  
            stats.append(np.mean(sample) )
        return stats

    def bootstraptest(self, setting, model_dir= None  ):
        _, test_loader = self._get_data(flag='test')
        # print('loading model from ' , model_dir)
        # ETTh1_96_720_llm_to_attnq
        # loaded_state_dict = torch.load(model_dir)
        
        if model_dir is not None : 
            self.model.load_state_dict(torch.load(model_dir), strict=False)
        else:
            self.model.load_state_dict(torch.load(os.path.join('./checkpoints/' + setting, 'checkpoint.pth')))
            
        preds = []
        trues = []
        self.model.eval()
        with torch.no_grad():
            for i, (batch_x, batch_y, batch_x_mark, batch_y_mark) in enumerate(test_loader):
                batch_x = batch_x.float().to(self.device)
                batch_y = batch_y.float().to(self.device)

                outputs = self.model(batch_x[:, -self.args.seq_len:, :])

                outputs_ensemble = outputs['outputs_time']
                
                outputs_ensemble = outputs_ensemble[:, -self.args.pred_len:, :]
                batch_y = batch_y[:, -self.args.pred_len:, :]

                pred = outputs_ensemble.detach().cpu().numpy()
                true = batch_y.detach().cpu().numpy()

                preds.append(pred)
                trues.append(true)

        preds = np.array(preds)
        trues = np.array(trues)
        # print('test shape1:', preds.shape, trues.shape)
        preds = preds.reshape(-1, preds.shape[-2], preds.shape[-1])
        trues = trues.reshape(-1, trues.shape[-2], trues.shape[-1])
        
        return np.mean(np.abs(preds - trues), axis=(1, 2))  ,np.mean((preds - trues) ** 2 , axis=(1, 2))

        # return self.boot_res(preds , trues )
        
        # print('test shape2:', preds.shape, trues.shape)
        # import time 
        # time.sleep(3000)
        # mae, mse, rmse, mape, mspe = metric(preds, trues)
        # print('mse:{}, mae:{}'.format(mse, mae))
        # return mae, mse

    def test_input_perturb(self, setting):
        test_data, test_loader = self._get_data(flag='test')
        self.model.load_state_dict(torch.load(os.path.join('./checkpoints/' + setting, 'checkpoint.pth')))

        self.model.eval()
        
        shuffle_types = ['sf_all' , 'sf_half' , 'ex_half' ]
        for shuffle_type in shuffle_types :
            print(shuffle_type) 
            preds = [] ;  trues = []
            with torch.no_grad():
                for i, (batch_x, batch_y, batch_x_mark, batch_y_mark) in enumerate(test_loader):
                    batch_x = batch_x.float().to(self.device)
                    batch_y = batch_y.float().to(self.device)
                    batch_x = perturb_sequence(batch_x , shuffle_type , patch_size = 16 , mask_ratio= 0.2  )
                     
                    outputs = self.model(batch_x[:, -self.args.seq_len:, :])

                    outputs_ensemble = outputs['outputs_time']
                    
                    outputs_ensemble = outputs_ensemble[:, -self.args.pred_len:, :]
                    batch_y = batch_y[:, -self.args.pred_len:, :]

                    pred = outputs_ensemble.detach().cpu().numpy()
                    true = batch_y.detach().cpu().numpy()

                    preds.append(pred)
                    trues.append(true)

            preds = np.array(preds)
            trues = np.array(trues)
            preds = preds.reshape(-1, preds.shape[-2], preds.shape[-1])
            trues = trues.reshape(-1, trues.shape[-2], trues.shape[-1])
            mae, mse, rmse, mape, mspe = metric(preds, trues)
            print('mse:{}, mae:{}'.format(mse, mae))

        shuffle_type= 'sf_patchs'
        print(shuffle_type)
        for patch_size in [4 , 8 , 16, 32] : 
            preds = [] ;  trues = []
            
            with torch.no_grad():
                for i, (batch_x, batch_y, batch_x_mark, batch_y_mark) in enumerate(test_loader):
                    batch_x = batch_x.float().to(self.device)
                    batch_y = batch_y.float().to(self.device)
                    batch_x = perturb_sequence(batch_x , shuffle_type , patch_size = patch_size , mask_ratio= 0.2  )
                        
                    outputs = self.model(batch_x[:, -self.args.seq_len:, :])

                    outputs_ensemble = outputs['outputs_time']
                    
                    outputs_ensemble = outputs_ensemble[:, -self.args.pred_len:, :]
                    batch_y = batch_y[:, -self.args.pred_len:, :]

                    pred = outputs_ensemble.detach().cpu().numpy()
                    true = batch_y.detach().cpu().numpy()

                    preds.append(pred)
                    trues.append(true)

            preds = np.array(preds)
            trues = np.array(trues)
            preds = preds.reshape(-1, preds.shape[-2], preds.shape[-1])
            trues = trues.reshape(-1, trues.shape[-2], trues.shape[-1])
            mae, mse, rmse, mape, mspe = metric(preds, trues)
            print(patch_size , 'mse:{}, mae:{}'.format(mse, mae))

        shuffle_type= 'masking'
        print(shuffle_type)
        for mask_ratio in [0.1, 0.2, 0.3 ,0.4 , 0.5 , 0.6 , 0.7 , 0.8]  : 
            preds = [] ;  trues = []
            with torch.no_grad():
                for i, (batch_x, batch_y, batch_x_mark, batch_y_mark) in enumerate(test_loader):
                    batch_x = batch_x.float().to(self.device)
                    batch_y = batch_y.float().to(self.device)
                    batch_x = perturb_sequence(batch_x , shuffle_type , patch_size = 1 , mask_ratio= mask_ratio  )
                        
                    outputs = self.model(batch_x[:, -self.args.seq_len:, :])

                    outputs_ensemble = outputs['outputs_time']
                    
                    outputs_ensemble = outputs_ensemble[:, -self.args.pred_len:, :]
                    batch_y = batch_y[:, -self.args.pred_len:, :]

                    pred = outputs_ensemble.detach().cpu().numpy()
                    true = batch_y.detach().cpu().numpy()

                    preds.append(pred)
                    trues.append(true)

            preds = np.array(preds)
            trues = np.array(trues)
            preds = preds.reshape(-1, preds.shape[-2], preds.shape[-1])
            trues = trues.reshape(-1, trues.shape[-2], trues.shape[-1])
            mae, mse, rmse, mape, mspe = metric(preds, trues)
            print(mask_ratio , 'mse:{}, mae:{}'.format(mse, mae))

