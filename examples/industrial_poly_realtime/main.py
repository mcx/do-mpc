#
#   This file is part of do-mpc
#
#   do-mpc: An environment for the easy, modular and efficient implementation of
#        robust nonlinear model predictive control
#
#   Copyright (c) 2014-2019 Sergio Lucia, Alexandru Tatulea-Codrean
#                        TU Dortmund. All rights reserved
#
#   do-mpc is free software: you can redistribute it and/or modify
#   it under the terms of the GNU Lesser General Public License as
#   published by the Free Software Foundation, either version 3
#   of the License, or (at your option) any later version.
#
#   do-mpc is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU Lesser General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with do-mpc.  If not, see <http://www.gnu.org/licenses/>.

import numpy as np
import matplotlib.pyplot as plt
from casadi import *
from casadi.tools import *
import pdb
import sys
sys.path.append('../../')
import do_mpc

import matplotlib.pyplot as plt
import pickle
import time

from template_model import template_model
from template_mpc import template_mpc
from template_simulator import template_simulator
from opcmodules import Server, Client
from opcmodules import RealtimeSimulator, RealtimeController
import multiprocessing as mp

model = template_model()

"""Defining the OPCUA settings"""
server_opts = {"_model":model,
               "_name":"Poly Reactor OPCUA",
               "_address":"opc.tcp://localhost:4840/freeopcua/server/",
               "_port": 4840,
               "_server_type": "basic",
               "_store_predictions": False,
               "_with_db": False}
opc_server = Server(server_opts)
if opc_server.created == True and opc_server.running == False: opc_server.start()

client_opts = {"_address":"opc.tcp://localhost:4840/freeopcua/server/",
               "_port": 4840,
               "_client_type": "simulator",
               "_namespace": opc_server.namespace}
opc_opts ={'_cycle_time': 1.0, '_opc_opts': client_opts}


rt_simulator = template_simulator(model,opc_opts)

client_opts['_client_type'] = "controller"
opc_opts ={'_cycle_time': 10.0, '_opc_opts': client_opts}

rt_controller = template_mpc(model,opc_opts)

# Set the initial state of mpc and simulator:
x0 = model._x(0)
#opc_client_sim.writeData(x0)

delH_R_real = 950.0
c_pR = 5.0

x0['m_W'] = 10000.0
x0['m_A'] = 853.0
x0['m_P'] = 26.5

x0['T_R'] = 90.0 + 273.15
x0['T_S'] = 90.0 + 273.15
x0['Tout_M'] = 90.0 + 273.15
x0['T_EK'] = 35.0 + 273.15
x0['Tout_AWT'] = 35.0 + 273.15
x0['accum_monom'] = 300.0
x0['T_adiab'] = x0['m_A']*delH_R_real/((x0['m_W'] + x0['m_A'] + x0['m_P']) * c_pR) + x0['T_R']


rt_controller.set_initial_state(x0, reset_history=True)
rt_simulator.set_initial_state(x0, reset_history=True)

# Initialize graphic:
graphics = do_mpc.graphics.Graphics()


time_list = []
for k in range(100):
    tic = time.time()
    #pdb.set_trace()
    rt_controller.run_once()
    #pdb.set_trace()
    rt_simulator.run_once()
    toc = time.time()
    time_list.append(toc-tic)

    #if True:
        #graphics.reset_axes()
        #graphics.plot_results(mpc.data, linewidth=3)
        #graphics.plot_predictions(mpc.data, linestyle='--', linewidth=1)
        #plt.show()
        #input('next step')

time_arr = np.array(time_list)
print('Total run-time: {tot:5.2f} s, step-time {mean:.3f}+-{std:.3f} s.'.format(tot=np.sum(time_arr), mean=np.mean(time_arr), std=np.sqrt(np.var(time_arr))))


fig, ax = plt.subplots(5, sharex=True)
plt.ion()
# Configure plot:
graphics.add_line(var_type='_x', var_name='T_R', axis=ax[0])
graphics.add_line(var_type='_x', var_name='accum_monom', axis=ax[1])
graphics.add_line(var_type='_u', var_name='m_dot_f', axis=ax[2])
graphics.add_line(var_type='_u', var_name='T_in_M', axis=ax[3])
graphics.add_line(var_type='_u', var_name='T_in_EK', axis=ax[4])

ax[0].set_ylabel('T_R [K]')
ax[1].set_ylabel('acc. monom')
ax[2].set_ylabel('m_dot_f')
ax[3].set_ylabel('T_in_M [K]')
ax[4].set_ylabel('T_in_EK [K]')

fig.align_ylabels()
plt.ion()

simu_lines = graphics.plot_results(rt_simulator.data)
plt.show()
input('Press any key to exit.')
