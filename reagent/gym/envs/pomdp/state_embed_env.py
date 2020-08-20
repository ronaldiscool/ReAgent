#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
"""
This file shows an example of using embedded states to feed to RL models in
partially observable environments (POMDPs). Embedded states are generated by a world
model which learns how to encode past n observations into a low-dimension
vector.Embedded states improve performance in POMDPs compared to just using
one-step observations as states because they encode more historical information
than one-step observations.
"""
import logging
from collections import deque
from typing import Optional

import gym
import numpy as np
import reagent.core.types as rlt
import torch
from gym.spaces import Box
from reagent.gym.envs.env_wrapper import EnvWrapper
from reagent.models.world_model import MemoryNetwork


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class StateEmbedEnvironment(gym.Env):
    def __init__(
        self,
        gym_env: EnvWrapper,
        mdnrnn: MemoryNetwork,
        max_embed_seq_len: int,
        state_min_value: Optional[float] = None,
        state_max_value: Optional[float] = None,
    ):
        self.env = gym_env
        self.unwrapped.spec = self.env.unwrapped.spec
        self.max_embed_seq_len = max_embed_seq_len
        self.mdnrnn = mdnrnn
        self.embed_size = self.mdnrnn.num_hiddens
        self.raw_state_dim = self.env.observation_space.shape[0]  # type: ignore
        self.state_dim = self.embed_size + self.raw_state_dim
        if isinstance(self.env.action_space, gym.spaces.Discrete):
            self.is_discrete_action = True
            self.action_dim = self.env.action_space.n
        elif isinstance(self.env.action_space, gym.spaces.Box):
            self.is_discrete_action = False
            self.action_dim = self.env.action_space.shape[0]

        self.action_space = self.env.action_space

        # only need to set up if needed
        if state_min_value is None or state_max_value is None:
            state_min_value = np.min(gym_env.observation_space.low)
            state_max_value = np.max(gym_env.observation_space.high)

        self.observation_space = Box(  # type: ignore
            low=state_min_value, high=state_max_value, shape=(self.state_dim,)
        )

        self.cur_raw_state = None
        self.recent_states = deque([], maxlen=self.max_embed_seq_len)  # type: ignore
        self.recent_actions = deque([], maxlen=self.max_embed_seq_len)  # type: ignore

    def seed(self, seed):
        self.env.seed(seed)

    def __getattr__(self, name):
        return getattr(self.env, name)

    @torch.no_grad()
    def embed_state(self, state):
        """ Embed state after either reset() or step() """
        assert len(self.recent_states) == len(self.recent_actions)
        old_mdnrnn_mode = self.mdnrnn.mdnrnn.training
        self.mdnrnn.mdnrnn.eval()

        # Embed the state as the hidden layer's output
        # until the previous step + current state
        if len(self.recent_states) == 0:
            mdnrnn_state = np.zeros((1, self.raw_state_dim))
            mdnrnn_action = np.zeros((1, self.action_dim))
        else:
            mdnrnn_state = np.array(list(self.recent_states))
            mdnrnn_action = np.array(list(self.recent_actions))

        mdnrnn_state = torch.tensor(mdnrnn_state, dtype=torch.float).unsqueeze(1)
        mdnrnn_action = torch.tensor(mdnrnn_action, dtype=torch.float).unsqueeze(1)
        mdnrnn_output = self.mdnrnn(
            rlt.FeatureData(mdnrnn_state), rlt.FeatureData(mdnrnn_action)
        )
        hidden_embed = (
            mdnrnn_output.all_steps_lstm_hidden[-1].squeeze().detach().cpu().numpy()
        )
        state_embed = np.hstack((hidden_embed, state))
        self.mdnrnn.mdnrnn.train(old_mdnrnn_mode)
        logger.debug(
            f"Embed_state\nrecent states: {np.array(self.recent_states)}\n"
            f"recent actions: {np.array(self.recent_actions)}\n"
            f"state_embed{state_embed}\n"
        )
        return state_embed

    def reset(self):
        next_raw_state = self.env.reset()
        self.recent_states = deque([], maxlen=self.max_embed_seq_len)
        self.recent_actions = deque([], maxlen=self.max_embed_seq_len)
        self.cur_raw_state = next_raw_state
        next_embed_state = self.embed_state(next_raw_state)
        return next_embed_state

    def step(self, action):
        if self.is_discrete_action:
            action_np = np.zeros(self.action_dim)
            action_np[action] = 1.0
        else:
            action_np = action
        self.recent_states.append(self.cur_raw_state)
        self.recent_actions.append(action_np)
        next_raw_state, reward, terminal, info = self.env.step(action)
        logger.debug("action {}, reward {}\n".format(action, reward))
        self.cur_raw_state = next_raw_state
        next_embed_state = self.embed_state(next_raw_state)
        return next_embed_state, reward, terminal, info
