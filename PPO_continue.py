import torch
import torch.nn as nn
from torch.distributions import MultivariateNormal
import gym
from gym.wrappers.time_limit import TimeLimit
from torch.distributions import Categorical
import numpy as np
import os
import matplotlib.pyplot as plt
from torch.utils.data.sampler import BatchSampler, SubsetRandomSampler
#using custom Environment
import swimmerChemo as chem

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
print(device)

class Memory:
    # structure to store data for each update
    def __init__(self):
        self.actions = []
        self.states = []
        self.logprobs = []
        self.rewards = []
        self.is_terminals = []

    def clear_memory(self):
        del self.actions[:]
        del self.states[:]
        del self.logprobs[:]
        del self.rewards[:]
        del self.is_terminals[:]


class ActorCritic(nn.Module):
    def __init__(self, state_dim, action_dim, action_std):
        super(ActorCritic, self).__init__()
        # action type: discrete or continuous
        # action mean range -1 to 1
        
        self.action_dim = action_dim
        self.action_var = torch.full((action_dim,), action_std * action_std).to(device)

  
        self.actor = nn.Sequential(
            nn.Linear(state_dim, 64),
            nn.Tanh(),
            nn.Linear(64, 32),
            nn.Tanh(),
            nn.Linear(32, 32),
            nn.Tanh(),
            nn.Linear(32, action_dim),
            nn.Tanh()
        )
        # critic
        self.critic = nn.Sequential(
            nn.Linear(state_dim, 64),
            nn.Tanh(),
            nn.Linear(64, 32),
            nn.Tanh(),
	        nn.Linear(32, 32),
            nn.Tanh(),
            nn.Linear(32, 1)
        )
        

    def forward(self):
        raise NotImplementedError

    def act(self, state, memory):
        # operations per decision time step
        action_mean = self.actor(state)
        cov_mat = torch.diag(self.action_var).to(device)

        dist = MultivariateNormal(action_mean, cov_mat)
        action = dist.sample()
        action_logprob = dist.log_prob(action)
        
        memory.states.append(state)
        memory.actions.append(action)
        memory.logprobs.append(action_logprob)

        return action.detach()

    def evaluate(self, state, action):
        action_mean = torch.squeeze(self.actor(state))

        action_var = self.action_var.expand_as(action_mean)
        cov_mat = torch.diag_embed(action_var).to(device)

        dist = MultivariateNormal(action_mean, cov_mat)

        action_logprobs = dist.log_prob(torch.squeeze(action))
        dist_entropy = dist.entropy()
        state_value = self.critic(state)

        return action_logprobs, torch.squeeze(state_value), dist_entropy


class PPO:
    def __init__(self, state_dim, action_dim, action_std, lr, betas, gamma, K_epochs, batch_size, eps_clip):
        self.lr = lr
        self.betas = betas
        self.gamma = gamma
        self.eps_clip = eps_clip
        self.K_epochs = K_epochs
        self.batch_size = batch_size
        self.mini_batch_size = 50
        self.policy = ActorCritic(state_dim, action_dim, action_std).to(device)
        self.optimizer = torch.optim.Adam(self.policy.parameters(), lr=lr, betas=betas)

        self.policy_old = ActorCritic(state_dim, action_dim,  action_std).to(device)
        self.policy_old.load_state_dict(self.policy.state_dict())

        self.MseLoss = nn.MSELoss()

    def select_action(self, state, memory):
        state = torch.FloatTensor(state.reshape(1, -1)).to(device)
        return self.policy_old.act(state, memory).cpu().data.numpy().flatten()

    def update(self, memory):
        # Monte Carlo estimate of rewards:
        rewards = []
        #loss_list = []
        # ------------------------------------------------------------------
        # compute discounted reward
        discounted_reward = 0
        for reward, is_terminal, state in zip(reversed(memory.rewards), reversed(memory.is_terminals),
                                              reversed(memory.states)):
            if is_terminal:
                value = self.policy.critic(state.squeeze())
                discounted_reward = value
            discounted_reward = reward + (self.gamma * discounted_reward)
            rewards.insert(0, discounted_reward)
        # ------------------------------------------------------------------

        rewards = torch.tensor(rewards).to(device)
        # Normalizing the rewards:
        # rewards = (rewards - rewards.mean()) / (rewards.std() + 1e-5)

        # convert list to tensor:
        old_states = torch.squeeze(torch.stack(memory.states).to(device)).detach()
        old_actions = torch.squeeze(torch.stack(memory.actions).to(device)).detach()
        old_logprobs = torch.squeeze(torch.stack(memory.logprobs)).to(device).detach()

        # Optimize policy for K epochs:
        for _ in range(self.K_epochs):
            for index in BatchSampler(SubsetRandomSampler(range(self.batch_size)), self.mini_batch_size, False):
            # Evaluating old actions and values :
                logprobs, state_values, dist_entropy = self.policy.evaluate(old_states[index], old_actions[index])

                # Finding the ratio (pi_theta / pi_theta__old):
                ratios = torch.exp(logprobs - old_logprobs[index].detach())

                # Finding Surrogate Loss:
                advantages = rewards[index] - state_values.detach()
                surr1 = ratios * advantages
                surr2 = torch.clamp(ratios, 1 - self.eps_clip, 1 + self.eps_clip) * advantages
                loss = -torch.min(surr1, surr2) + 0.5 * self.MseLoss(state_values, rewards[index]) - 0.01 * dist_entropy

                # take gradient step
                self.optimizer.zero_grad()
                loss.mean().backward()
                self.optimizer.step()
            #print (loss)
            #loss_list.append(loss.mean().backward())
      #  print('Loss {}', sum(loss_list)/len(loss_list))
        # Copy new weights into old policy:
        self.policy_old.load_state_dict(self.policy.state_dict())


def main(k):
    path_cur = os.getcwd()
    path = path_cur + '/direction/{}'.format(k)
    if not os.path.exists(path):
        os.makedirs(path)
    env_name = "swimmerChemo-v0" # used when creating the en
    render = False  # render the environment in training if true
    # solved_reward = 100         # stop training if avg_reward > solved_reward
    log_interval = 20  # print avg reward in the interval
    # max_episodes = 10000  # max training episodes
    max_episodes = 10000  # max training episodes
    max_timesteps = 100  # max timesteps in one episode

    update_timestep = 2000  # update policy every n timesteps
    action_std = 0.5  # constant std for action distribution (Multivariate Normal)
    K_epochs = 20  # update policy for K epochs
    batch_size = 2000  # num of transitions sampled from replay buffer
    eps_clip = 0.2  # clip parameter for PPO
    gamma = 0.99  # discount factor

    lr = 0.0003  # parameters for Adam optimizer
    betas = (0.9, 0.999)

    random_seed = None

    # creating environment
    env = chem.ChemoSwimmerV0(dt = 1, length = 50)
    env = TimeLimit(env, max_episode_steps=100)

    # get observation and action dimensions from the environment
    state_dim = env.observation_space.shape[0]
    action_dim = env.action_space.shape[0]

    if random_seed:
        print("Random Seed: {}".format(random_seed))
        torch.manual_seed(random_seed)
        env.seed(random_seed)
        np.random.seed(random_seed)

    memory = Memory()
    ppo = PPO(state_dim, action_dim, action_std, lr, betas, gamma, K_epochs, batch_size, eps_clip)

    # logging variables
    running_reward = 0
    avg_length = 0
    time_step = 0
    i_epslst = []
    running_rewardlst = []
    # training loop
    for i_episode in range(1, max_episodes + 1):

        #print ('Episode{}'.format(i_episode))
        state = env.reset()
        for t in range(max_timesteps):
            time_step += 1
            # Running policy_old:
            action = ppo.select_action(state, memory)
            state, reward, done, _ = env.step(action)
            #print ('Episode{}, step{}'.format(i_episode,t))
            # Storing reward and is_terminals:
            memory.rewards.append(reward)
            memory.is_terminals.append(done)

            # update if it is time
            # ------------------------------------------------------------------
            if time_step % update_timestep == 0:
                ppo.update(memory)
                memory.clear_memory()
                time_step = 0
            # ------------------------------------------------------------------
            running_reward += reward
            if render:
                env.render()
            # break if episode ends
            if done:
                break
        avg_length += t


        # save every 50 episodes
        if i_episode % 50 == 0:
            torch.save(ppo.policy.state_dict(), path + '/PPO_{}_direction{:06d}.pth'.format(env_name, i_episode))

            # ------------------------------------------------------------------
        # logging
       
        if i_episode % log_interval == 0:
            avg_length = int(avg_length / log_interval)
            running_reward = ((running_reward / log_interval))
            print('Episode {} \t Avg length: {} \t Avg reward: {}'.format(i_episode, avg_length, running_reward))
            i_epslst.append(i_episode)
            running_rewardlst.append(running_reward)
            np.savetxt('./episode.csv',i_epslst)
            np.savetxt('./rewardlist.csv',running_rewardlst)
            running_reward = 0
            avg_length = 0

    print(i_epslst, running_rewardlst)
    np.savetxt(path+f'epslst{k}.csv', i_epslst)
    np.savetxt(path+f'rlst{k}.csv', running_rewardlst)
    plt.plot(i_epslst, running_rewardlst)
    plt.xlabel('Number of episodes')
    plt.ylabel('Reward')
    plt.savefig('Evolution of r during the training.png')
    #plt.show()
        # ------------------------------------------------------------------



if __name__ == '__main__':
    print('direction150')
    # training for 25 times
    #for k in range(9, 25):
    main(1)
