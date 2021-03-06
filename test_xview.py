import argparse
import json

from torch.utils.data import DataLoader

from utils.parse_config_xview import *
from models_xview import *

from utils.datasets_xview import *
# from utils.datasets_xview_backup import *
from utils.utils_xview import *


# import sys
# sys.path.append('.')


class MyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        else:
            return super(MyEncoder, self).default(obj)


#fixme
# def get_val_imgid_by_name(image_name, opt=None):
#     image_id_name_maps = json.load(
#         open(os.path.join(opt.label_dir, 'all_image_ids_names_dict_{}cls.json'.format(opt.class_num))))
#     img_ids = [int(k) for k in image_id_name_maps.keys()]
#     img_names = [v for v in image_id_name_maps.values()]
#     return img_ids[img_names.index(image_name)]


def get_val_imgid_by_name(path, name):
    json_img_id_file = glob.glob(os.path.join(path, 'xview_val*_img_id_map.json'))[0]
    img_id_map = json.load(open(json_img_id_file))
    imgIds = [id for id in img_id_map.values()]
    return img_id_map[name]
    # # print(path)
    # val_files = pd.read_csv(path, header=None).to_numpy()
    # # print('val_files 0', val_files[0])
    # val_names = [os.path.basename(vf[0]) for vf in val_files]
    # img_id = val_names.index(name)
    # return img_id


def test(cfg,
         data,
         weights=None,
         batch_size=16,
         img_size=416,
         conf_thres=0.001,
         iou_thres=0.5,  # for nms
         save_json=False,
         model=None,
         dataloader=None,
         opt=None):
    # Initialize/load model and set device
    if model is None:
        device = torch_utils.select_device(opt.device, batch_size=batch_size)
        verbose = opt.task == 'test'

        # Remove previous
        for f in glob.glob('test_batch*.jpg'):
            os.remove(f)

        # Initialize model
        model = Darknet(cfg, img_size).to(device)

        # Load weights
        attempt_download(weights)
        if weights.endswith('.pt'):  # pytorch format
            model.load_state_dict(torch.load(weights, map_location=device)['model'])
        else:  # darknet format
            _ = load_darknet_weights(model, weights)

        if torch.cuda.device_count() > 1:
            model = nn.DataParallel(model)
    else:  # called by train.py
        device = next(model.parameters()).device  # get model device
        verbose = False

    # Configure run
    data = parse_data_cfg(data)
    nc = int(data['classes'])  # number of classes
    # fixme
    path = data['valid']  # path to test images
    lbl_path = data['valid_label']
    # path = data['valid_rare']  # path to test images
    # lbl_path = data['valid_rare_label']
    names = load_classes(data['names'])  # class names
    iouv = torch.linspace(0.5, 0.95, 10).to(device)  # iou vector for mAP@0.5:0.95
    iouv = iouv[0].view(1)  # for mAP@0.5
    niou = iouv.numel()

    # Dataloader
    if dataloader is None:
        dataset = LoadImagesAndLabels(path, lbl_path, img_size, batch_size, rect=True, cache_labels=True)  #
        batch_size = min(batch_size, len(dataset))
        dataloader = DataLoader(dataset,
                                batch_size=batch_size,
                                num_workers=min([os.cpu_count(), batch_size if batch_size > 1 else 0, 8]),
                                pin_memory=True,
                                collate_fn=dataset.collate_fn)

    seen = 0
    model.eval()
    # fixme
    # coco91class = coco80_to_coco91_class()
    xview_classes = np.arange(nc)
    s = ('%20s' + '%10s' * 6) % ('Class', 'Images', 'Targets', 'P', 'R', 'mAP@0.5', 'F1')
    p, r, f1, mp, mr, map, mf1 = 0., 0., 0., 0., 0., 0., 0.
    loss = torch.zeros(3)
    jdict, stats, ap, ap_class = [], [], [], []
    # fixme
    for batch_i, (imgs, targets, paths, shapes) in enumerate(tqdm(dataloader, desc=s)):
        imgs = imgs.to(device).float() / 255.0  # uint8 to float32, 0 - 255 to 0.0 - 1.0
        targets = targets.to(device)
        # print('targets--', targets.shape)
        # print('paths--', paths)
        # print('shapes', shapes)
        # print(targets)
        # exit(0)
        _, _, height, width = imgs.shape  # batch size, channels, height, width

        # Plot images with bounding boxes
        if batch_i == 0 and not os.path.exists('test_1gpu.jpg'):
            plot_images(imgs=imgs, targets=targets, paths=paths, fname='test_batch0.jpg')

        # Disable gradients
        with torch.no_grad():
            # Run model
            inf_out, train_out = model(imgs)  # inference and training outputs

            # Compute loss
            if hasattr(model, 'hyp'):  # if model has loss hyperparameters
                loss += compute_loss(train_out, targets, model)[1][:3].cpu()  # GIoU, obj, cls

            # Run NMS
            output = non_max_suppression(inf_out, conf_thres=conf_thres, iou_thres=iou_thres)

        # Statistics per image
        for si, pred in enumerate(output):
            # print('si', si, targets[si])
            labels = targets[targets[:, 0] == si, 1:]
            nl = len(labels)
            tcls = labels[:, 0].tolist() if nl else []  # target class
            seen += 1

            if pred is None:
                if nl:
                    stats.append((torch.zeros(0, 1), torch.Tensor(), torch.Tensor(), tcls))
                continue

            # Append to text file
            # with open('test.txt', 'a') as file:
            #    [file.write('%11.5g' * 7 % tuple(x) + '\n') for x in pred]

            # Clip boxes to image bounds
            clip_coords(pred, (height, width))

            # Append to pycocotools JSON dictionary
            if save_json:
                # [{"image_id": 42, "category_id": 18, "bbox": [258.15, 41.29, 348.26, 243.78], "score": 0.236}, ...
                # fixme
                # print('paths[si]', paths[si]) #  /media/lab/Yang/data/xView_YOLO/images/608/1094_11.jpg
                # image_id = int(Path(paths[si]).stem.split('_')[-1])

                image_name = paths[si].split('/')[-1]
                # image_id = get_val_imgid_by_name(opt.base_dir, image_name)

                box = pred[:, :4].clone()  # xyxy
                scale_coords(imgs[si].shape[1:], box, shapes[si][0], shapes[si][1])  # to original shape
                box = xyxy2xywh(box)  # xywh
                box[:, :2] -= box[:, 2:] / 2  # xy center to top-left corner # xtlytlwh
                for di, d in enumerate(pred):
                    #fixme
                    # jdict.append({'image_id': image_id,
                    #               'category_id': xview_classes[int(d[5])],
                    #               'bbox': [floatn(x, 3) for x in box[di]],
                    #               'score': floatn(d[4], 5)})  # conf
                    jdict.append({'image_name': image_name, # image_id,
                                  'category_id': xview_classes[int(d[5])],
                                  'bbox': [floatn(x, 3) for x in box[di]],
                                  'score': floatn(d[4], 5)})

            # Assign all predictions as incorrect
            correct = torch.zeros(len(pred), niou, dtype=torch.bool)
            if nl:
                detected = []  # target indices
                tcls_tensor = labels[:, 0]

                # target boxes
                tbox = xywh2xyxy(labels[:, 1:5]) * torch.Tensor([width, height, width, height]).to(device)

                # Per target class
                for cls in torch.unique(tcls_tensor):
                    ti = (cls == tcls_tensor).nonzero().view(-1)  # prediction indices
                    pi = (cls == pred[:, 5]).nonzero().view(-1)  # target indices

                    # Search for detections
                    if len(pi):
                        # Prediction to target ious
                        ious, i = box_iou(pred[pi, :4], tbox[ti]).max(1)  # best ious, indices

                        # Append detections
                        for j in (ious > iouv[0]).nonzero():
                            d = ti[i[j]]  # detected target
                            if d not in detected:
                                detected.append(d)
                                correct[pi[j]] = ious[j] > iouv  # iou_thres is 1xn
                                if len(detected) == nl:  # all targets already located in image
                                    break

            # Append statistics (correct, conf, pcls, tcls)
            # pred (x1, y1, x2, y2, object_conf, conf, class)
            stats.append((correct, pred[:, 4].cpu(), pred[:, 5].cpu(), tcls))

    # Compute statistics
    stats = [np.concatenate(x, 0) for x in list(zip(*stats))]  # to numpy
    if len(stats):
        p, r, ap, f1, ap_class = ap_per_class(*stats, pr_path=opt.result_dir, pr_name=opt.name)
        # if niou > 1:
        #       p, r, ap, f1 = p[:, 0], r[:, 0], ap[:, 0], ap.mean(1)  # average across ious
        #fixme --yang.xu
        if niou > 1:
            p, r, ap, f1 = p[:, 0], r[:, 0], ap.mean(1), ap[:, 0]  # [P, R, AP@0.5:0.95, AP@0.5]
        mp, mr, map, mf1 = p.mean(), r.mean(), ap.mean(), f1.mean()
        nt = np.bincount(stats[3].astype(np.int64), minlength=nc)  # number of targets per class
    else:
        nt = torch.zeros(1)

    # Print results
    pf = '%20s' + '%10.3g' * 6  # print format
    print(pf % ('all', seen, nt.sum(), mp, mr, map, mf1))

    # Print results per class
    if verbose and nc > 1 and len(stats):
        for i, c in enumerate(ap_class):
            print(pf % (names[c], seen, nt[c], p[i], r[i], ap[i], f1[i]))

    # Save JSON
    if save_json and map and len(jdict):
        # fixme
        # img_names = [os.path.basename(x) for x in dataloader.dataset.img_files]
        #fixme
        # img_id_maps = json.load(
        #     open(os.path.join(opt.label_dir, 'all_image_ids_names_dict_{}cls.json'.format(opt.class_num))))
        # img_id_list = [k for k in img_id_maps.keys()]
        # img_name_list = [v for v in img_id_maps.values()]
        # imgIds = [img_id_list[img_name_list.index(v)] for v in img_name_list if
        #           v in img_names]  # note: index is the same as the keys
        # sids = set(imgIds)
        # print('imgIds', len(imgIds), 'sids', len(sids))

        # json_img_id_file = glob.glob(os.path.join(opt.base_dir, 'xview_val*_img_id_map.json'))[0]
        # img_id_map = json.load(open(json_img_id_file))
        # imgIds = [id for id in img_id_map.values()]

        # imgIds = [get_val_imgid_by_name(na) for na in img_names]
        # sids = set(imgIds)
        # print('imgIds', len(imgIds), 'sids', len(sids))
        # imgIds = np.arange(len(output))

        result_json_file = 'results_{}.json'.format(opt.name)
        with open(os.path.join(opt.result_dir, result_json_file), 'w') as file:
            # json.dump(jdict, file)
            json.dump(jdict, file, ensure_ascii=False, indent=2, cls=MyEncoder)

        # try:
        #     from pycocotools.coco import COCO
        #     from pycocotools.cocoeval import COCOeval
        # except:
        #     print('WARNING: missing pycocotools package, can not compute official COCO mAP. See requirements.txt.')
        #
        #     # https://github.com/cocodataset/cocoapi/blob/master/PythonAPI/pycocoEvalDemo.ipynb
        #     # fixme
        #     # cocoGt = COCO(glob.glob('../coco/annotations/instances_val*.json')[0])  # initialize COCO ground truth api
        #     cocoGt = COCO(glob.glob(os.path.join(opt.base_dir, '*_xtlytlwh.json'))[0])
        #     cocoDt = cocoGt.loadRes(os.path.join(opt.result_dir, result_json_file))  # initialize COCO pred api
        #
        #     cocoEval = COCOeval(cocoGt, cocoDt, 'bbox')
        #     cocoEval.params.imgIds = imgIds  # [:32]  # only evaluate these images
        #     cocoEval.evaluate()
        #     cocoEval.accumulate()
        #     cocoEval.summarize()
        #     mf1, map = cocoEval.stats[:2]  # update to pycocotools results (mAP@0.5:0.95, mAP@0.5)

    # Return results
    maps = np.zeros(nc) + map
    for i, c in enumerate(ap_class):
        maps[c] = ap[i]
    return (mp, mr, map, mf1, *(loss.cpu() / len(dataloader)).tolist()), maps
    # return (mp, mr, map, mf1, *(loss / len(dataloader)).tolist()), maps
    # return (mp, mr, map, mf1, *(loss / len(dataloader)).tolist())


def get_opt(dt=None, sr=None, comments=''):
    parser = argparse.ArgumentParser(prog='test.py')

    parser.add_argument('--cfg', type=str, default='cfg/yolov3-spp-{}cls_syn.cfg', help='*.cfg path')
    # parser.add_argument('--data', type=str, default='/home/jovyan/work/data/syn_background_gt_bbox/syn_mixed_seed17.data', help='*.data path')
    # parser.add_argument('--weights', type=str, default='/home/jovyan/work/code/yxu-yolov3-xview/weights/1_cls/syn_mixed_seed17/hgiou1_1gpu_seed17_2020-06-22_22.40/last_seed17.pt', help='path to weights file')
    parser.add_argument('--data', type=str, default='/home/jovyan/work/code/yxu-yolov3-xview/data_wnd/wnd_px10_seed17.data', help='*.data path')
    parser.add_argument('--weights', type=str, default='/home/jovyan/work/code/yxu-yolov3-xview/weights/1_cls/syn_mixed_seed17/hgiou1_1gpu_seed17_2020-07-09_05.22/last_seed17.pt', help='path to weights file')

    parser.add_argument('--batch-size', type=int, default=8, help='size of each image batch')
    parser.add_argument('--img_size', type=int, default=608, help='inference size (pixels)')

    parser.add_argument('--class_num', type=int, default=1, help='class number')  # 60 6
    parser.add_argument('--label_dir', type=str, default='/media/lab/Yang/data/xView_YOLO/labels/', help='*.json path')
    parser.add_argument('--weights_dir', type=str, default='weights/{}_cls/{}_seed{}/', help='to save weights path')
    parser.add_argument('--result_dir', type=str, default='result_output/{}_cls/{}_seed{}/{}/', help='to save result files path')
    parser.add_argument("--syn_ratio", type=float, default=sr, help="ratio of synthetic data: 0 0.25, 0.5, 0.75")
    parser.add_argument('--syn_display_type', type=str, default=dt, help='syn_texture0, syn_color0, syn_texture, syn_color, syn_mixed, syn')
    parser.add_argument('--base_dir', type=str, default='data_xview/{}_cls/{}/', help='without syn data path')

    parser.add_argument('--conf-thres', type=float, default=0.1, help='0.1 0.001 object confidence threshold')
    parser.add_argument('--iou-thres', type=float, default=0.5, help='0.5 IOU threshold for NMS')
    parser.add_argument('--save_json', action='store_true', default=True, help='save a cocoapi-compatible JSON results file')
    parser.add_argument('--task', default='test', help="'test', 'study', 'benchmark'")
    parser.add_argument('--device', default='', help='device id (i.e. 0 or 0,1) or cpu')
    parser.add_argument('--name', default='', help='name')

    opt = parser.parse_args()
    # opt.save_json = opt.save_json or any([x in opt.data for x in ['xview.data']])
    opt.cfg = opt.cfg.format(opt.class_num)

    return opt


if __name__ == '__main__':

    # display_type = 'syn_background'
    # syn_ratio = 0.1
    # opt = get_opt(display_type, syn_ratio)
    # comments = 'syn_only'
    # display_type = ['syn_color', 'syn_texture', 'syn_mixed']
    # # for dt in display_type:
    #     opt = get_opt(dt, None, comments)
    #     opt.save_json = True # opt.save_json or any([x in opt.data for x in ['xview_{}_{}.data'.format(opt.syn_display_type, opt.syn_ratio)]])
    #     opt.weights_dir = opt.weights_dir.format(opt.class_num, opt.syn_display_type, opt.syn_ratio if opt.syn_ratio is not None else comments)
    #     opt.writer_dir = opt.writer_dir.format(opt.class_num, opt.syn_display_type, opt.syn_ratio if opt.syn_ratio is not None else comments)
    #     opt.data = opt.data.format(opt.class_num, opt.syn_display_type, opt.syn_ratio if opt.syn_ratio is not None else comments)
    #     opt.result_dir = opt.result_dir.format(opt.class_num, opt.syn_display_type, opt.syn_ratio if opt.syn_ratio is not None  else comments)
    #     opt.weights = opt.weights.format(opt.class_num, opt.syn_display_type, opt.syn_ratio, opt.syn_display_type, opt.syn_ratio if opt.syn_ratio is not None  else comments)
    #     opt.label_dir = opt.label_dir + '{}/{}_cls/{}_{}/'.format(opt.img_size, opt.class_num, opt.syn_display_type, opt.syn_ratio if opt.syn_ratio is not None  else comments)

    #     if opt.task == 'test':  # task = 'test', 'study', 'benchmark'
    #         # Test
    #         opt.data = '/media/lab/Yang/code/yolov3/data_xview/1_cls/xview_syn_background_0_px6whr4_ng0.data'
    #         opt.weights = glob.glob(os.path.join(opt.weights_dir, '*_{}*'.format(comments), 'best_{}_{}.pt'.format(dt, comments)))[0]
    #         opt.name = dt
    #         # opt.weights = glob.glob(os.path.join(opt.weights_dir, '*_{}'.format(comments), 'backup90.pt' ))[0]# 'best_{}_{}.pt'.format(dt, comments)))[0]
    #         # opt.name = dt + '2'
    #         test(opt.cfg,
    #              opt.data,
    #              opt.weights,
    #              opt.batch_size,
    #              opt.img_size,
    #              opt.conf_thres,
    #              opt.iou_thres,
    #              opt.save_json, opt=opt)
    #
    #     elif opt.task == 'benchmark':
    #         # mAPs at 320-608 at conf 0.5 and 0.7
    #         y = []
    #         for i in [320, 416, 512, 608]:
    #             for j in [0.5, 0.7]:
    #                 t = time.time()
    #                 r = test(opt.cfg, opt.data, opt.weights, opt.batch_size, i, opt.conf_thres, j, opt.save_json)[0]
    #                 y.append(r + (time.time() - t,))
    #         np.savetxt(opt.result_dir + 'benchmark.txt', y, fmt='%10.4g')  # y = np.loadtxt('study.txt')
    #
    #     elif opt.task == 'study':
    #         # Parameter study
    #         y = []
    #         x = np.arange(0.4, 0.9, 0.05)
    #         for i in x:
    #             t = time.time()
    #             r = test(opt.cfg, opt.data, opt.weights, opt.batch_size, opt.img_size, opt.conf_thres, i, opt.save_json,
    #                      opt=opt)
    #             y.append(r + (time.time() - t,))
    #         np.savetxt(opt.result_dir + 'study.txt', y, fmt='%10.4g')  # y = np.loadtxt('study.txt')
    #
    #         # Plot
    #         fig, ax = plt.subplots(3, 1, figsize=(6, 6))
    #         y = np.stack(y, 0)
    #         ax[0].plot(x, y[:, 2], marker='.', label='mAP@0.5')
    #         ax[0].set_ylabel('mAP')
    #         ax[1].plot(x, y[:, 3], marker='.', label='mAP@0.5:0.95')
    #         ax[1].set_ylabel('mAP')
    #         ax[2].plot(x, y[:, -1], marker='.', label='time')
    #         ax[2].set_ylabel('time (s)')
    #         for i in range(3):
    #             ax[i].legend()
    #             ax[i].set_xlabel('iou_thr')
    #         fig.tight_layout()
    #         plt.savefig(opt.result_dir + 'study.jpg', dpi=200)

    # from pycocotools.coco import COCO
    # from pycocotools.cocoeval import COCOeval
    # cocoGt = COCO(opt.label_dir + 'xViewval_{}_{}cls_xtlytlwh.json'.format(opt.img_size, opt.class_num))
    # cocoDt = cocoGt.loadRes(opt.result_dir + 'results.json')  # initialize COCO pred api
    # cocoEval = COCOeval(cocoGt, cocoDt, 'bbox')

    '''
    test for syn_xveiw_background_*  on original
    '''
    # import train_syn_xview_background_seeds_loop_1cls as tsl
    # # comments = ['syn_xview_background_texture', 'syn_xview_background_color', 'syn_xview_background_mixed']
    # comments = ['syn_xview_bkg_certain_models_texture', 'syn_xview_bkg_certain_models_color', 'syn_xview_bkg_certain_models_mixed']
    # # comments = ['syn_xview_bkg_px20whr4_certain_models_texture', 'syn_xview_bkg_px20whr4_certain_models_color', 'syn_xview_bkg_px20whr4_certain_models_mixed']
    # seeds = [17]
    # #
    # #5  all        94       219    0.0699     0.466     0.175     0.122
    # for sd in seeds[:1]:
    #     for cmt in comments[:2]:
    #         opt = tsl.get_opt(sd, cmt, Train=False)
    #         opt.result_dir = opt.result_dir.format(opt.class_num, cmt, sd , '{}_hgiou1_seed{}'.format('test_on_original', sd))
    #         if not os.path.exists(opt.result_dir):
    #             os.mkdir(opt.result_dir)
    #
    #         opt.weights = glob.glob(os.path.join(opt.weights_dir.format(opt.class_num, cmt, sd, '*_hgiou1_seed{}'.format(sd)), 'best_{}_seed{}.pt'.format(cmt, sd)))[-1]
    #         opt.data = 'data_xview/{}_cls/{}/{}_seed{}.data'.format(opt.class_num, 'px6whr4_ng0_seed{}'.format(sd), 'xview_px6whr4_ng0', sd)
    #         # opt.data = 'data_xview/{}_cls/{}/{}_seed{}.data'.format(opt.class_num, 'px20whr4_seed{}'.format(sd), 'xview_px20whr4', sd)
    #         print(opt.data)
    #         opt.name = 'seed{}_on_original'.format(opt.seed)
    #         opt.task = 'test'
    #         opt.save_json = True
    #         test(opt.cfg,
    #              opt.data,
    #              opt.weights,
    #              opt.batch_size,
    #              opt.img_size,
    #              opt.conf_thres,
    #              opt.iou_thres,
    #              opt.save_json, opt=opt)

    '''
    test for syn_xveiw_background_*  on original
    '''
    # import train_syn_xview_background_seeds_loop_1cls as tsl
    # # comments = ['syn_xview_background_texture', 'syn_xview_background_color', 'syn_xview_background_mixed']
    # # comments = ['syn_xview_bkg_certain_models_texture', 'syn_xview_bkg_certain_models_color', 'syn_xview_bkg_certain_models_mixed']
    # # comments = ['syn_xview_bkg_px20whr4_certain_models_texture', 'syn_xview_bkg_px20whr4_certain_models_color', 'syn_xview_bkg_px20whr4_certain_models_mixed']
    # comments = ['syn_xview_bkg_px23whr4_scale_models_texture', 'syn_xview_bkg_px23whr4_scale_models_color', 'syn_xview_bkg_px23whr4_scale_models_mixed']
    # seeds = [17]
    # #
    # #5  all        94       219    0.0699     0.466     0.175     0.122
    # for sd in seeds[:1]:
    #     for cmt in comments[:1]:
    #         opt = tsl.get_opt(sd, cmt, Train=False)
    #         opt.result_dir = opt.result_dir.format(opt.class_num, cmt, sd , '{}_hgiou1_seed{}'.format('test_on_original', sd))
    #         if not os.path.exists(opt.result_dir):
    #             os.mkdir(opt.result_dir)
    #
    #         opt.weights = glob.glob(os.path.join(opt.weights_dir.format(opt.class_num, cmt, sd, '*_hgiou1_seed{}'.format(sd)), 'best_{}_seed{}.pt'.format(cmt, sd)))[0]
    #         # opt.data = 'data_xview/{}_cls/{}/{}_seed{}.data'.format(opt.class_num, 'px6whr4_ng0_seed{}'.format(sd), 'xview_px6whr4_ng0', sd)
    #         # opt.data = 'data_xview/{}_cls/{}/{}_seed{}.data'.format(opt.class_num, 'px20whr4_seed{}'.format(sd), 'xview_px20whr4', sd)
    #         opt.data = 'data_xview/{}_cls/{}/{}_seed{}.data'.format(opt.class_num, 'px23whr4_seed{}'.format(sd), 'xview_px23whr4', sd)
    #         print(opt.data)
    #         opt.name = 'seed{}_on_original'.format(opt.seed)
    #         opt.task = 'test'
    #         opt.save_json = True
    #         test(opt.cfg,
    #              opt.data,
    #              opt.weights,
    #              opt.batch_size,
    #              opt.img_size,
    #              opt.conf_thres,
    #              opt.iou_thres,
    #              opt.save_json, opt=opt)


    '''
    test for syn_xveiw_background_*_with_model
    '''
    # # comments = ['syn_xview_background_texture', 'syn_xview_background_color', 'syn_xview_background_mixed']
    # # comments = ['syn_xview_bkg_certain_models_texture', 'syn_xview_bkg_certain_models_color', 'syn_xview_bkg_certain_models_mixed']
    # # comments = ['syn_xview_bkg_px20whr4_certain_models_texture', 'syn_xview_bkg_px20whr4_certain_models_color', 'syn_xview_bkg_px20whr4_certain_models_mixed']
    # comments = ['syn_xview_bkg_px23whr4_scale_models_texture', 'syn_xview_bkg_px23whr4_scale_models_color', 'syn_xview_bkg_px23whr4_scale_models_mixed']
    # comments = ['syn_xview_bkg_px23whr4_small_models_color']
    # comments = ['syn_xview_bkg_px23whr4_small_fw_models_color']
    # comments = ['syn_xview_bkg_px23whr3_6groups_models_color', 'syn_xview_bkg_px23whr3_6groups_models_mixed']
    comments = ['syn_xview_bkg_px23whr3_rnd_bwratio_models_color', 'syn_xview_bkg_px23whr3_rnd_bwratio_models_mixed']
    px_thres = 23
    whr_thres = 3 # 4
    # hyp_cmt = 'hgiou1_fitness'
    hyp_cmt = 'hgiou1_mean_best'
    seeds = [17]
    for sd in seeds:
        for cmt in comments:
            opt = get_opt(comments=cmt)
            opt.result_dir = opt.result_dir.format(opt.class_num, cmt, sd , '{}_{}_seed{}_with_model'.format('test_on_original', hyp_cmt, sd))
            if not os.path.exists(opt.result_dir):
                os.makedirs(opt.result_dir)

            # opt.weights = glob.glob(os.path.join(opt.weights_dir.format(opt.class_num, cmt, sd), '*_{}_seed{}'.format(hyp_cmt, sd), 'best_{}_seed{}.pt'.format(cmt, sd)))[-1]
            print(opt.weights)
            # opt.data = 'data_xview/{}_cls/{}/{}_seed{}_with_model.data'.format(opt.class_num, 'px6whr4_ng0_seed{}'.format(sd), 'xview_px6whr4_ng0', sd)
            # opt.data = opt.data.format(opt.class_num, 'px{}whr{}_seed{}'.format(px_thres, whr_thres, sd), 'xview_px{}whr{}'.format(px_thres, whr_thres), sd)
            print(opt.data)
            opt.name = '{}_seed{}_on_xview_with_model'.format(cmt, sd)
            test(opt.cfg,
                 opt.data,
                 opt.weights,
                 opt.batch_size,
                 opt.img_size,
                 opt.conf_thres,
                 opt.iou_thres,
                 opt.save_json, opt=opt)

    '''
    test for xview_syn_xview_bkg_* with model
    '''
    # # # # comments = ['xview_syn_xview_bkg_texture', 'xview_syn_xview_bkg_color', 'xview_syn_xview_bkg_mixed']
    # # # # comments = ['xview_syn_xview_bkg_certain_models_texture', 'xview_syn_xview_bkg_certain_models_color', 'xview_syn_xview_bkg_certain_models_mixed']
    # # # # comments = ['xview_syn_xview_bkg_px20whr4_certain_models_texture', 'xview_syn_xview_bkg_px20whr4_certain_models_color', 'xview_syn_xview_bkg_px20whr4_certain_models_mixed']
    # comments = ['xview_syn_xview_bkg_px23whr4_scale_models_texture', 'xview_syn_xview_bkg_px23whr4_scale_models_color', 'xview_syn_xview_bkg_px23whr4_scale_models_mixed']
    # # comments = [ 'xview_syn_xview_bkg_px23whr4_small_models_color', 'xview_syn_xview_bkg_px23whr4_small_models_mixed']
    # base_cmt = 'px23whr4_seed{}'
    # # comments = [ 'xview_syn_xview_bkg_px23whr3_small_models_color', 'xview_syn_xview_bkg_px23whr3_small_models_mixed']
    # # comments = ['xview_syn_xview_bkg_px23whr3_6groups_models_color', 'xview_syn_xview_bkg_px23whr3_6groups_models_mixed']
    # # base_cmt = 'px23whr3_seed{}'
    # # hyp_cmt = 'hgiou1_fitness'
    # hyp_cmt = 'hgiou1'
    # sd=17
    # #5  all   94       219    0.0699     0.466     0.175     0.122
    # for cmt in comments:
    #     opt = get_opt(comments=cmt)
    #     opt.result_dir = opt.result_dir.format(opt.class_num, cmt, sd, 'test_on_xview_with_model_{}_seed{}_1xSyn'.format(hyp_cmt, sd))
    #     if not os.path.exists(opt.result_dir):
    #         os.makedirs(opt.result_dir)
    #     opt.weights = glob.glob(os.path.join(opt.weights_dir.format(opt.class_num, cmt, sd),  '*_{}_seed{}_1xSyn'.format(hyp_cmt, sd), 'best_seed{}_1xSyn.pt'.format(sd)))[-1]
    #     # opt.weights = glob.glob(os.path.join(opt.weights_dir.format(opt.class_num, cmt, sd),  '*_{}_seed{}_1xSyn'.format(hyp_cmt, sd), 'backup200.pt'.format(sd)))[-1]
    #     print(opt.weights)
    #     opt.data = 'data_xview/{}_cls/{}_seed{}/{}_seed{}_1xSyn_with_model.data'.format(opt.class_num, cmt, sd, cmt, sd)
    #     # opt.data = 'data_xview/{}_cls/{}_seed{}/{}_seed{}_with_model.data'.format(opt.class_num, cmt, sd, cmt, sd)
    #     print(opt.data)
    #     opt.name = 'seed{}_on_xview_with_model'.format(sd)
    #     opt.base_dir = opt.base_dir.format(opt.class_num, base_cmt.format(sd))
    #     print(opt.base_dir)
    #     test(opt.cfg,
    #          opt.data,
    #          opt.weights,
    #          opt.batch_size,
    #          opt.img_size,
    #          opt.conf_thres,
    #          opt.iou_thres,
    #          opt.save_json, opt=opt)

    '''
    test for xview_px6whr4_ng0_* with model
    '''
    # # # comments = ['px6whr4_ng0']
    # # # comments = ['px20whr4']
    # # comments = ['px23whr4']
    # # base_cmt = 'px23whr4_seed{}'
    # comments = ['px23whr3']
    # base_cmt = 'px23whr3_seed{}'
    # hyp_cmt = 'hgiou1_fitness'
    # # hyp_cmt = 'hgiou1'
    # sd=17
    # #5  all   94       219    0.0699     0.466     0.175     0.122
    # for cmt in comments:
    #     opt = get_opt(comments=cmt)
    #     opt.result_dir = opt.result_dir.format(opt.class_num, cmt, sd, 'test_on_xview_with_model_{}_seed{}'.format(hyp_cmt, sd))
    #     if not os.path.exists(opt.result_dir):
    #         os.mkdir(opt.result_dir)
    #     print(opt.weights_dir.format(opt.class_num, cmt, sd,  '*_{}_seed{}'.format(hyp_cmt, sd)))
    #     opt.weights = glob.glob(os.path.join(opt.weights_dir.format(opt.class_num, cmt, sd),  '*_{}_seed{}'.format(hyp_cmt, sd), 'best_{}_seed{}.pt'.format(cmt, sd)))[-1]
    #     print(opt.weights)
    #
    #     opt.data = 'data_xview/{}_cls/{}_seed{}/xview_{}_seed{}_with_model.data'.format(opt.class_num, cmt, sd, cmt, sd)
    #     print(opt.data)
    #     opt.name = 'seed{}_on_xview_with_model'.format(sd)
    #     opt.base_dir = opt.base_dir.format(opt.class_num, base_cmt.format(sd))
    #     test(opt.cfg,
    #          opt.data,
    #          opt.weights,
    #          opt.batch_size,
    #          opt.img_size,
    #          opt.conf_thres,
    #          opt.iou_thres,
    #          opt.save_json, opt=opt)


