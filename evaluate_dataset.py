from cmath import inf
from curses import noecho
from distutils.command.config import config
import sys
from wsgiref.validate import InputWrapper
if '/opt/ros/kinetic/lib/python2.7/dist-packages' in sys.path:
    sys.path.remove('/opt/ros/kinetic/lib/python2.7/dist-packages')
import argparse
import imageio
from copy import deepcopy, copy
import json
import matplotlib.pyplot as plt

parser = argparse.ArgumentParser()
parser.add_argument("--seed", type=int, default=1)
parser.add_argument("--config", type=str, required=True)
parser.add_argument("--version", type=str, default="", help="exp name")
parser.add_argument("--gpu", type=str, default="0,0", help="Simulation and evaluation GPU IDs")
parser.add_argument("--stop", action='store_true', default=False)
parser.add_argument("--forget", action='store_true', default=False)
parser.add_argument("--diff", choices=['random', 'easy', 'medium', 'hard', 'multi'], default='hard')
parser.add_argument("--dataset", choices=['gibson', 'mp3d'], default='gibson')
parser.add_argument("--split", choices=['val', 'train', 'min_val'], default='val')
parser.add_argument('--eval-ckpt', type=str, required=True)
parser.add_argument('--render', action='store_true', default=False)
parser.add_argument('--record', choices=['0','1','2','3'], default='0') # 0: no record; 1: env.render; 2: goal emb att scores; 3: GAT env global node att cores
parser.add_argument('--th', type=str, default='0.75') # s_th
parser.add_argument('--record-dir', type=str, default='data/video_dir')

args = parser.parse_args()
args.record = int(args.record)
args.th = float(args.th)
import os
# if args.gpu != 'cpu':
#     os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu
os.environ['GLOG_minloglevel'] = "3"
os.environ['MAGNUM_LOG'] = "quiet"
os.environ['HABITAT_SIM_LOG'] = "quiet"
import numpy as np
import torch
np.random.seed(args.seed)
torch.manual_seed(args.seed)
if args.gpu != 'cpu':
    torch.cuda.manual_seed(args.seed)
torch.backends.cudnn.enable = True
torch.set_num_threads(5)

from env_utils.make_env_utils import add_panoramic_camera
import habitat
from habitat import make_dataset
from env_utils.task_search_env import SearchEnv, MultiSearchEnv
from configs.default import get_config, CN
import time
import cv2
import gzip
import quaternion as q

val_dir = "image-goal-nav-dataset/val/*"
multi_goal_val_dir = "image-goal-nav-dataset/val_multigoal/*"

def eval_config(args):
    val_scene_ep_list = glob.glob(val_dir if 'multi' not in args.diff else multi_goal_val_dir)
    total_ep_num = 0
    for scene_file in val_scene_ep_list:
        with gzip.open(scene_file) as fp:
            episode_list = json.loads(fp.read())
        scene_name = scene_file.split('/')[-1][:-len('.json.gz')]
        scene_ep_num = len([ep for ep in episode_list if args.diff in ep['info']['difficulty']])
        total_ep_num += scene_ep_num
        print("{} contains {} episodes".format(scene_name, scene_ep_num))
    
    print("Diff %s Total %d eps"%(args.diff, total_ep_num))

    if args.dataset == "gibson":
        base_task = "configs/vistargetnav_gibson.yaml"
    elif args.dataset == "mp3d":
        base_task = "configs/vistargetnav_mp3d.yaml"
    
    config = get_config(args.config, args.version, create_folders=False, base_task_config_path=base_task)
    config.defrost()
    config.use_depth = config.TASK_CONFIG.use_depth = True
    config.DIFFICULTY = args.diff
    habitat_api_path = os.path.join(os.path.dirname(habitat.__file__), '../')
    #print(args.config)
    config.TASK_CONFIG = add_panoramic_camera(config.TASK_CONFIG, normalize_depth=True)
    config.TASK_CONFIG.DATASET.SPLIT = args.split if 'gibson' in config.TASK_CONFIG.DATASET.DATA_PATH else 'test'
    config.TASK_CONFIG.DATASET.SCENES_DIR = os.path.join(habitat_api_path, config.TASK_CONFIG.DATASET.SCENES_DIR)
    config.TASK_CONFIG.DATASET.DATA_PATH = os.path.join(habitat_api_path, config.TASK_CONFIG.DATASET.DATA_PATH)
    
    if 'COLLISIONS' not in config.TASK_CONFIG.TASK.MEASUREMENTS:
        config.TASK_CONFIG.TASK.MEASUREMENTS += ['COLLISIONS']

    dataset = make_dataset(config.TASK_CONFIG.DATASET.TYPE)

    if config.TASK_CONFIG.DATASET.CONTENT_SCENES == ['*']:
        scenes = dataset.get_scenes_to_load(config.TASK_CONFIG.DATASET)
    else:
        scenes = config.TASK_CONFIG.DATASET.CONTENT_SCENES

    config.TASK_CONFIG.DATASET.CONTENT_SCENES = scenes
    ep_per_env = int(np.ceil(total_ep_num / len(scenes)))
    config.TASK_CONFIG.ENVIRONMENT.ITERATOR_OPTIONS.MAX_SCENE_REPEAT_EPISODES = ep_per_env
    if args.stop:
        config.ACTION_DIM = 4
        config.TASK_CONFIG.TASK.POSSIBLE_ACTIONS= ["STOP", "MOVE_FORWARD", "TURN_LEFT", "TURN_RIGHT"]
    else:
        config.ACTION_DIM = 3
        config.TASK_CONFIG.TASK.POSSIBLE_ACTIONS = ["MOVE_FORWARD", "TURN_LEFT", "TURN_RIGHT"]
        config.TASK_CONFIG.TASK.SUCCESS.TYPE = "Success_woSTOP"

    if args.forget:
        config.memory.FORGET= True
    
    # NOTE: See line 500 in task_search_env.py for why 'GOAL_INDEX' should be included in MEASUREMENTS
    # NOTE: MEASUREMENTS should contain items in such order ['GOAL_INDEX', 'DISTANCE_TO_GOAL', 'SUCCESS', 'SPL','TOP_DOWN_MAP', 'COLLISIONS'],
    # since 'SUCCESS' depends on 'DISTANCE_TO_GOAL' and 'GOAL_INDEX'
    config.TASK_CONFIG.TASK.MEASUREMENTS.insert(0,'GOAL_INDEX')

    if 'multi' in args.diff:
        config.TASK_CONFIG.TASK.SUCCESS.TYPE = 'Success_MultiGoal'
    
    config.freeze()
    return config

def load(ckpt):
    new_state_dict, env_global_node = None, None
    if ckpt != 'none':
        sd = torch.load(ckpt,map_location=torch.device('cpu'))
        state_dict = sd['state_dict']
        new_state_dict = {}

        env_global_node = sd.get("env_global_node", None)
        ckpt_config = sd.get("config", None)

        for key in state_dict.keys():
            if 'actor_critic' in key:
                new_state_dict[key[len('actor_critic.'):]] = state_dict[key]
            else:
                new_state_dict[key] = state_dict[key]
        
        return (new_state_dict, env_global_node, ckpt_config)

from runner import *
#TODO: ADD runner in the config file e.g. config.runner = 'VGMRunner' or 'BaseRunner'
def evaluate(eval_config, ckpt):
    if args.record > 0:
        if not os.path.exists(os.path.join(args.record_dir, eval_config.VERSION)):
            os.mkdir(os.path.join(args.record_dir, eval_config.VERSION))

        VIDEO_DIR = os.path.join(args.record_dir, eval_config.VERSION + '_video_' + ckpt.split('/')[-1] + '_' +str(time.ctime()))
        if not os.path.exists(VIDEO_DIR): os.mkdir(VIDEO_DIR)

    # if args.record > 1:
    #     OTHER_DIR = os.path.join(args.record_dir, eval_config.VERSION + '_other_' + ckpt.split('/')[-1] + '_' + str(time.ctime()))
    #     if not os.path.exists(OTHER_DIR): os.mkdir(OTHER_DIR)

    state_dict, env_global_node, ckpt_config = load(ckpt)

    if ckpt_config is not None:
        task_config = eval_config.TASK_CONFIG
        ckpt_config.defrost()
        task_config.defrost()
        ckpt_config.TASK_CONFIG = task_config
        ckpt_config.runner = eval_config.runner
        ckpt_config.AGENT_TASK = 'search'
        ckpt_config.DIFFICULTY = eval_config.DIFFICULTY
        ckpt_config.ACTION_DIM = eval_config.ACTION_DIM
        ckpt_config.memory = eval_config.memory
        ckpt_config.scene_data = eval_config.scene_data
        ckpt_config.WRAPPER = eval_config.WRAPPER
        ckpt_config.REWARD_METHOD = eval_config.REWARD_METHOD
        ckpt_config.ENV_NAME = eval_config.ENV_NAME
        ckpt_config.VERSION = eval_config.VERSION
        ckpt_config.POLICY = eval_config.POLICY

        for k, v in eval_config.items():
            if k not in ckpt_config:
                ckpt_config.update({k:v})
            if isinstance(v, CN):
                for kk, vv in v.items():
                    if kk not in ckpt_config[k]:
                        ckpt_config[k].update({kk: vv})

        ckpt_config.update({"SIMULATOR_GPU_ID": args.gpu[0]})
        ckpt_config.update({"TORCH_GPU_ID": args.gpu[-1]})

        ckpt_config.freeze()
        eval_config = ckpt_config

    eval_config.defrost()
    eval_config.th = args.th

    eval_config.record = False # record from this side , not in env
    eval_config.render_map = args.record > 0 or args.render or 'hand' in args.config

    if args.record == 3:
        assert eval_config.GCN.WITH_ENV_GLOBAL_NODE and 'GAT' in eval_config.GCN.TYPE, "[Error] This model does not support drawing GAT att scores!"

    # Multi-goal testing is much more suitable for evaluating the performance of Forgetting MEchanism
    if 'multi' in args.diff:
        eval_config.ENV_NAME = "MultiSearchEnv"
    
    eval_config.noisy_actuation = True
    eval_config.freeze()

    # VGMRunner
    runner = eval(eval_config.runner)(eval_config, env_global_node=env_global_node, return_features=True)

    print('====================================')
    print('Version Name: ', eval_config.VERSION)
    print('Task config path: ', args.dataset)
    print('Runner: ', eval_config.runner)
    print('Policy: ', eval_config.POLICY)
    print('Num params: {}'.format(sum(param.numel() for param in runner.parameters())))
    print('Difficulty: ', eval_config.DIFFICULTY)
    print('Stop action: ', 'True' if eval_config.ACTION_DIM==4 else 'False')
    print('Env gloabl node: ', str(eval_config.GCN.WITH_ENV_GLOBAL_NODE))

    if eval_config.memory.FORGET:
        num_forgotten_nodes = "{}%".format(int(100 * eval_config.memory.RANK_THRESHOLD)) if eval_config.memory.RANK_THRESHOLD < 1 else "{}".format(int(eval_config.memory.RANK_THRESHOLD))
        if eval_config.memory.RANK == 'bottom':
            print('Forgetting: {} \n\t Start forgetting after {} nodes are collected\n\t Nodes in the bottom {} will be forgotten'.format(str(eval_config.memory.FORGET), eval_config.memory.TOLERANCE, num_forgotten_nodes))
        elif eval_config.memory.RANK == 'top':
            print('Forgetting: {} \n\t Start forgetting after {} nodes are collected\n\t Nodes in the top {} will be remembered'.format(str(eval_config.memory.FORGET), eval_config.memory.TOLERANCE, num_forgotten_nodes))
    else:
        print('Forgetting: False')
    print('GCN encoder type: ', eval_config.GCN.TYPE)
    print('Fusion method: ', str(eval_config.FUSION_TYPE))
    print('====================================')
    
    runner.eval()
    if torch.cuda.device_count() > 0:
        device = torch.device("cuda:"+str(eval_config.TORCH_GPU_ID))
        runner.to(device)
    #runner.load(state_dict)

    try:
        runner.load(state_dict)
    except:
        raise
        agent_dict = runner.agent.state_dict()
        new_sd = {k: v for k, v in state_dict.items() if k in agent_dict.keys() and (v.shape == agent_dict[k].shape)}
        agent_dict.update(new_sd)
        runner.load(agent_dict)

    # Segmentation fault (core dumped) occurred here
    env = eval(eval_config.ENV_NAME)(eval_config) # SearchEnv in task_search_env.py

    # habitat_env is class Env in custom_habitat_env.py
    env.habitat_env._sim.seed(args.seed)
    
    if runner.need_env_wrapper:
        env = runner.wrap_env(env,eval_config) # initialize a GraphWrapper in runner

    val_scene_ep_list = glob.glob(val_dir if 'multi' not in args.diff else multi_goal_val_dir)
    
    global scene_ep_dict
    global self
    from habitat_sim.utils.common import quat_from_coeffs

    scene_ep_dict = {}
    total_ep_num = 0
    for scene_file in val_scene_ep_list:
        print(scene_file)
        with gzip.open(scene_file) as fp:
            episode_list = json.loads(fp.read())
        scene_name = scene_file.split('/')[-1][:-len('.json.gz')]
        scene_ep_dict[scene_name] = [ep for ep in episode_list if args.diff in ep['info']['difficulty']]
        total_ep_num += len(scene_ep_dict[scene_name])
    print("Diff %s Total %d eps"%(args.diff, total_ep_num))
    total_episode_id = 0

    from habitat.tasks.nav.nav import NavigationEpisode, NavigationGoal
    def next_episode(episode_id, scene_id):
        scene_name = scene_id.split('/')[-1][:-len('.glb')]
        if episode_id >= len(scene_ep_dict[scene_name]):
            return None, False
        else:
            episode_info = scene_ep_dict[scene_name][episode_id]
            new_episode = NavigationEpisode(**episode_info)
            new_episode.goals = [NavigationGoal(position=g['position']) for g in new_episode.goals]
            # start_rotation is saved in a format [x,y,z,w] in .json files
            # NOTE:quat_from_coeffs and quat_to_coeffs go between np.quaternion and a numpy array or list in [x,y,z,w] format
            # NOTE: np.quaternion will be however printed like quaternion(w, x, y, z)
            new_episode.start_rotation = q.as_float_array(quat_from_coeffs(new_episode.start_rotation))
            
            return new_episode, True

    env.env.habitat_env.get_next_episode_search = next_episode # GraphWrapper.SearchEnv(inherited from RLEnv).Env.get_next_episode_search

    result = {}
    result['config'] = eval_config
    result['args'] = args
    result['version_name'] = eval_config.VERSION
    result['start_time'] = time.ctime()
    result['noisy_action'] = env.noise
    scene_dict = {}
    render_check = False
    avg_decision_time = [0,0]
    
    done_type_list = ["success", "get stuck", "distance2goal is inf"]

    with torch.no_grad():
        ep_list = []
        total_success, total_spl, total_success_timesteps = [], [], []
        avg_decision_time_per_ep = [0, 0]
        decision_time_stats = {}
        reached_goal_idx_stats = {}
        complete_success_lst = []

        env.init_map_settings()
        
        for episode_id in range(total_ep_num):
            # env is env_utils/env_graph_wrapper.GraphWrapper
            # after reset, the starting position will be saved as the first node in the topological map
            obs = env.reset() 

            if render_check == False:
                if obs['panoramic_rgb'].sum() == 0 :
                    print('NO RENDERING!!!!!!!!!!!!!!!!!! YOU SHOULD CHECK YOUT DISPLAY SETTING')
                else:
                    render_check=True
            
            runner.reset()

            scene_name = env.current_episode.scene_id.split('/')[-1][:-4]

            #if scene_name not in ["Mosquito", "Greigsville"]: continue

            if scene_name not in scene_dict.keys():
                scene_dict[scene_name] = {'success': [], 'spl': [], 'avg_step': []}
            done = True
            reward = None
            info = None

            if args.record > 0:
                img, _ = env.render('rgb') # the GraphWrapper calls the method "self.render" of superclass Wrapper (defined in gym/core.py), and Wrapper.render calls SeachEnv.render (defined in task_search_env.py)
                imgs = [img]
                waipoint_maps = []
            step = 0

            while True:
                
                # Please copy waypoints before calling runner.step to obtain att_features, since att_features belong to old waypoints
                waypoint_pose = copy(env.graph.node_position_list[0]) if args.record >= 2 else None

                # obs contains the following keys:
                #  'rgb_0-11', 'depth_0-11', 'panoramic_rgb', 'panoramic_depth', 'target_goal': torch.tensor of shape [1, 64, 252, 4] in float range [0,1], 'episode_id', 'step', 'position', 'rotation', 'target_pose', 'distance', 'have_been',
                # 'target_dist_score', 'global_memory', 'global_act_memory', 'global_mask', 'global_A', 'global_time', 'forget_mask', 'localized_idx']
                #print(obs["target_goal"][0,:,:,0:3].max(), obs["target_goal"][0,:,:,0:3].min(), obs["target_goal"][0,:,:,0:3].dtype)
                # cv2.imshow("2",obs["target_goal"][0,:,:,0:3].cpu().numpy())
                # cv2.waitKey(5)
                action, att_features, decision_time = runner.step(obs, reward, done, info, env)
                avg_decision_time_per_ep[0] += decision_time
                avg_decision_time_per_ep[1] += 1

                num_nodes = int(obs['global_mask'].sum().item())
                if decision_time_stats.get(num_nodes, None) is None:
                    decision_time_stats[num_nodes] = [decision_time, 1]
                else:
                    decision_time_stats[num_nodes][0] += decision_time
                    decision_time_stats[num_nodes][1] += 1
                
                # Forget some less important nodes
                # att_features is a dict {'goal_attn': 1 x 1 x num_nodes, 'curr_attn': 1 x 1 x num_nodes, 'GAT_attn'}
                env.forget_node(att_features['goal_attn'])
                
                #print(step, ': action ', action)
                # info is a dict containing ['goal_index', 'num_goals', 'distance_to_goal', 'success', 'spl', 'collisions', 'done_type', 'length', 'episode', 'step']
                obs, reward, done, info = env.step(action) # Graph_wrapper.step()
                step += 1
                # print(["{:.4f}".format(x) for x in obs['position'][0].tolist()],
                # "{}/{} goal:".format(info['goal_index']['curr_goal_index'] + 1,info['num_goals']),
                # ["{:.4f}".format(x) for x in obs['target_pose'][0].tolist()],
                # 'dist2goal: {:.4f}'.format(info['distance_to_goal']),
                # 'SR: {:.4f}'.format(info['success']),
                # 'SPL: {:.4f}'.format(info['spl']),
                # 'reward: {:.5f}'.format(reward))

                # input(att_features['goal_attn'].shape)
                if args.record > 0:
                    # node_position_list is a nested list containing several lists, each of which contains a series of waypoints and is maintained for a single episode
                    # each waypoint is a 3d vec whose meaning is unclear, but its form is the same as that of nav_point = env.env.habitat_env.sim.pathfinder.get_random_navigable_point()
                    if args.record == 2:
                        att_scores = att_features['goal_attn'][0,0] # batch size and query length are both 1, so we remove these two dimensions
                    elif args.record == 3:
                        att_scores = att_features['GAT_attn'] # 1 x num_nodes
                    else:
                        att_scores = None
                    
                    img, waipoint_map = env.render(
                        'rgb',
                        waypoint_pose=waypoint_pose,
                        att_features=att_scores,
                        imshow=args.render) # <class 'numpy.ndarray'> (450, 950, 3)
                    
                    if waipoint_map is not None:
                        waipoint_maps.append(waipoint_map)
                        # cv2.imshow('attn', waipoint_map)
                        # cv2.waitKey(5)
                    imgs.append(img)

                # if args.render:
                #     env.render('human')
                if done:
                    break

            spl = info['spl']
            if np.isnan(spl):
                spl = 0.0
            
            scene_dict[scene_name]['success'].append(info['success'])
            scene_dict[scene_name]['spl'].append(spl)
            total_success.append(info['success'])
            total_spl.append(spl)
            if info['success']:
                scene_dict[scene_name]['avg_step'].append(step)
                total_success_timesteps.append(step)
                complete_success_lst.append("{}_ep{}".format(scene_name, episode_id))

            goals = env.habitat_env.current_episode.goals

            final_reached_goal_idx = env.habitat_env.task.measurements.measures['goal_index'].goal_index
            reached_goal_idx_stats[final_reached_goal_idx] = reached_goal_idx_stats.get(final_reached_goal_idx, 0) + 1

            ep_list.append({'house': scene_name,
                            'ep_id': env.current_episode.episode_id,
                            'start_pose': [env.current_episode.start_position, env.current_episode.start_rotation],
                            'num_goals': env.env.habitat_env._num_goals,
                            'target_pose': [x.position  for x in goals],#env.habitat_env.task.sensor_suite.sensors['target_goal'].goal_pose,
                            'final_reached_goal_idx': final_reached_goal_idx, # counting from 1
                            'total_step': step,
                            'collision': info['collisions']['count'] if isinstance(info['collisions'], dict) else info['collisions'],
                            'success': info['success'],
                            'spl': spl,
                            'distance_to_next_goal': info['distance_to_goal'],
                            #'target_distance': target_dist_list
                                })
            
            if args.record > 0:
                video_name = os.path.join(VIDEO_DIR,'%04d_%s_success=%.1f_spl=%.1f.mp4'%(episode_id, scene_name, info['success'], spl))
                with imageio.get_writer(video_name, fps=30) as writer:
                    im_shape = imgs[-1].shape
                    for im in imgs:
                        if (im.shape[0] != im_shape[0]) or (im.shape[1] != im_shape[1]):
                            im = cv2.resize(im, (im_shape[1], im_shape[0]))
                        writer.append_data(im.astype(np.uint8))
                    writer.close()
                
                if len(waipoint_maps) >0 and waipoint_maps[0] is not None:
                    video_name = os.path.join(VIDEO_DIR,'waypoint_map_%04d_%s_success=%.1f_spl=%.1f.mp4'%(episode_id, scene_name, info['success'], spl))
                    with imageio.get_writer(video_name, fps=30) as writer:
                        im_shape = waipoint_maps[-1].shape
                        for im in waipoint_maps:
                            if (im.shape[0] != im_shape[0]) or (im.shape[1] != im_shape[1]):
                                im = cv2.resize(im, (im_shape[1], im_shape[0]))
                            writer.append_data(im.astype(np.uint8))
                        writer.close()

            print('[{:04d}/{:04d}] {} success {:.4f}, spl {:.4f}, steps {:.2f}, final_reached_goal_idx {} || total success {:.4f}, spl {:.4f}, success time step {:.2f}, avg decision time {:.4f}s'.format(episode_id,
                                                          total_ep_num,
                                                          scene_name,
                                                          info['success'],
                                                          spl,
                                                          step,
                                                          final_reached_goal_idx,
                                                          np.array(total_success).mean(),
                                                          np.array(total_spl).mean(),
                                                          np.array(total_success_timesteps).mean(),
                                                          avg_decision_time_per_ep[0] / avg_decision_time_per_ep[1],
                                                          ))

            avg_decision_time[0] += avg_decision_time_per_ep[0]
            avg_decision_time[1] += avg_decision_time_per_ep[1]

           # if episode_id == 5 : break
    result['detailed_info'] = ep_list
    result['each_house_result'] = {}
    success = []
    spl = []

    for scene_name in scene_dict.keys():
        mean_success = np.array(scene_dict[scene_name]['success']).mean().item()
        mean_spl = np.array(scene_dict[scene_name]['spl']).mean().item()
        mean_step = np.array(scene_dict[scene_name]['avg_step']).mean().item()
        result['each_house_result'][scene_name] = {'success': mean_success, 'spl': mean_spl, 'avg_step': mean_step}
        print('SCENE %s: success %.4f, spl %.4f, avg steps %.2f'%(scene_name, mean_success,mean_spl, mean_step))
        success.extend(scene_dict[scene_name]['success'])
        spl.extend(scene_dict[scene_name]['spl'])
    
    result['avg_success'] = np.array(success).mean().item()
    result['avg_spl'] = np.array(spl).mean().item()
    result['avg_timesteps'] = np.array(total_success_timesteps).mean().item()
    result['avg_decision_time'] = avg_decision_time[0] / avg_decision_time[1]
    result['reached_goal_idx_stats'] = reached_goal_idx_stats
    result['complete_success_ep'] = complete_success_lst
    decision_time = {}
    for k in decision_time_stats.keys():
        decision_time[k] = decision_time_stats[k][0] / decision_time_stats[k][1]
    
    result['decision_time_stats'] = decision_time # stores the time used by the Policy.act() compared with the different number of nodes contained in the topological map  
    print('================================================')
    print('avg success : %.4f'%result['avg_success'])
    print('avg spl : %.4f'%result['avg_spl'])
    print('avg timesteps : %.4f'% result['avg_timesteps'])
    print('avg decision time [sec] : %.4f' % result['avg_decision_time'])
    env.close()
    return result

if __name__=='__main__':

    import joblib
    import glob
    cfg = eval_config(args)
    if os.path.isdir(args.eval_ckpt):
        print('eval_ckpt ', args.eval_ckpt, ' is directory')
        ckpts = [os.path.join(args.eval_ckpt,x) for x in sorted(os.listdir(args.eval_ckpt))]
        ckpts.reverse()
    elif os.path.exists(args.eval_ckpt):
        ckpts = args.eval_ckpt.split(",")
    else:
        ckpts = [x for x in sorted(glob.glob(args.eval_ckpt+'*'))]
        ckpts.reverse()   
    print('evaluate total {} ckpts'.format(len(ckpts)))
    print(ckpts)

    eval_results_dir = "eval_results"
    if not os.path.exists(eval_results_dir):
        os.mkdir(eval_results_dir)
    
    this_exp_dir = os.path.join(eval_results_dir, cfg.VERSION)
    if not os.path.exists(this_exp_dir):
        os.mkdir(this_exp_dir)
    
    for ckpt in ckpts:
        if 'ipynb' in ckpt or 'pt' not in ckpt: continue
        print('============================', ckpt.split('/')[-1], '==================')

        result = evaluate(cfg, ckpt)

        # simple eval results
        ckpt_name = ckpt.split('/')[-1].replace('.','').replace('pth','')

        each_scene_results_txt = os.path.join(this_exp_dir, "{}_{}_{}.txt".format(cfg.VERSION, ckpt_name, args.diff))
        with open(each_scene_results_txt, 'w') as f:
            lines = ["Avg SR: {:.4f}, Avg SPL: {:.4f}, Avg success timestep: {:.4f}, Avg decision time: {:.4f}\n".format(result['avg_success'], result['avg_spl'], result['avg_timesteps'], result['avg_decision_time'])]
            lines.append("Results of each scene:\n")
            for k, v in result['each_house_result'].items():
                this_scene = result['each_house_result'][k]
                lines.append("{}: SR {:.4f}, SPL {:.4f}, Avg step {:.2f}\n".format(k, this_scene['success'], this_scene['spl'], this_scene['avg_step']))
            
            lines.append("\nDecision time [sec] when the topological map contains different number of nodes:\n")
            for num_node in result['decision_time_stats'].keys():
                lines.append("{}: {:.4f}\n".format(num_node, result['decision_time_stats'][num_node]))
            
            lines.append("\nHow many goals the agent reached successfully:\n")
            for num_goal in result['reached_goal_idx_stats']:
                lines.append("{} goals: {} episodes\n".format(num_goal, result['reached_goal_idx_stats'][num_goal]))
            
            lines.append("\nCompletely successful episodes:\n")
            for ep in result['complete_success_ep']:
                lines.append(ep+'\n')

            f.writelines(lines)
        print("save brief eval results to", each_scene_results_txt)

        # detailed eval results
        eval_data_name = os.path.join(this_exp_dir, '{}_{}_{}.dat.gz'.format(cfg.VERSION, ckpt_name, args.diff))
        if os.path.exists(eval_data_name):
            data = joblib.load(eval_data_name)
            if cfg.VERSION in data.keys():
                data[cfg.VERSION].update({ckpt + '_{}'.format(time.time()): result})
            else:
                data.update({cfg.VERSION: {ckpt + '_{}'.format(time.time()): result}})
        else:
            data = {cfg.VERSION: {ckpt + '_{}'.format(time.time()): result}}
        joblib.dump(data, eval_data_name)

        print("save detailed eval results to", eval_data_name)
