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
from casadi import *
from casadi.tools import *
import pdb
import itertools
import time

import do_mpc.data
import do_mpc.optimizer
from do_mpc.tools.indexedproperty import IndexedProperty
from scipy.signal import cont2discrete
from scipy.linalg import solve_discrete_are,solve_continuous_are
import do_mpc.model

class LQR:
    """Linear Quadratic Regulator.
    
    Use this class to configure and run the LQR controller
    according to the previously configured :py:class:`do_mpc.model.Model` instance.
    
    **Configuration and setup:**
    
    Configuring and setting up the LQR controller involves the following steps:
    
    1. Use :py:func:`set_param` to configure the :py:class:`LQR` instance.
    
    2. Set the objective of the control problem with :py:func:`set_objective`
    
    3. To finalize the class configuration call :py:meth:`setup`.
    
    After configuring LQR controller, the controller can be made to operate in two modes. 
    
    1. Set point tracking mode - can be enabled by setting the setpoint using :py:func:`set_point` (default)
    
    2. Input Rate Penalization mode - can be enabled by executing :py:func:`input_rate_penalization` and also passing sufficent arguments to the :py:func:`set_objective`
    
    .. note::
        During runtime call :py:func:`make_step` with the current state :math:`x` to obtain the optimal control input :math:`u`.
        During runtitme call :py:func:`set_point` with the set points of input :math:`u_{ss}`, states :math:`x_{ss}` and algebraic states :math:`z_{ss}` in order to update the respective set points.
    """
    def __init__(self,model):
        self.model = model
        
        assert model.flags['setup'] == True, 'Model for MPC was not setup. After the complete model creation call model.setup().'
        self.model_type = model.model_type
        
        #Parameters necessary for setting up LQR
        self.data_fields = [
            't_sample',
            'n_horizon',
            'mode',
            'conv_method'
            ]
        #Initialize prediction horizon for the problem
        self.n_horizon = 0
        
        #Initialize sampling time for continuous time system to discrete time system conversion
        self.t_sample = 0
        
        self.conv_method = 'zoh'
        #Initialize mode of LQR
        self.mode = 'setPointTrack'
        
        self.flags = {'linear':False,
                      'setup':False}
        
        self.u0 = np.array([[]])
        
        self.xss = None
        self.uss = None
    def continuous_to_discrete_time(self):
        """Converts continuous time to discrete time system.
        
        This method utilizes the exisiting function in scipy library called :math:`cont2discrete` to convert continuous time to discrete time system.This method 
        allows the user to specify the type of discretization. For more details about the function `click here`_ .
         
        .. _`click here`: https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.cont2discrete.html
            
        where :math:`A_{discrete}` and :math:`B_{discrete}` are the discrete state matrix and input matrix repectively and :math:`t_{sample}`
        is the sampling time. Sampling time is set using :py:func:`set_param`.
        
        .. warning::
            sampling time is zero when not specified or not required
        
        :return: State :math:`A_{discrete}` and input :math:`B_{discrete}` matrices as a tuple
        :rtype: numpy.ndarray

        """
        #Checks the model type
        if self.model.model_type == 'continuous':
            warnings.warn('sampling time is {}'.format(self.t_sample))
            C = np.identity(self.model.n_x)
            D = np.zeros((self.model.n_x,self.model.n_u))

            dis_sys = cont2discrete((self.A,self.B,C,D), self.t_sample, self.conv_method)

            A_dis = dis_sys[0]
            B_dis = dis_sys[1]
            
            #Initializing new model type
            self.model_type = 'discrete'
            
        return A_dis,B_dis
    
    def state_matrix(self):
        """Extracts state matix from ODE or difference equation.
        
        This method utilizes the API :py:func:`jacobian` from casadi in order to obtain the repective state matrix.

        :raises exception: given model is not linear. Lineaize the model using :py:func:`model.linearize` after :py:meth:`model.setup`.

        :return: Extracted state matrix :math:`A`
        :rtype: casadi.SX

        """
        #Calculating jacobian with respect to state variables
        A = jacobian(self.model._rhs,self.model.x)
        
        #Check whether the obtained matrix is constant
        if A.is_constant() == True:
            self.flags['linear'] = True
        elif A.is_constant() == False:
            self.flags['linear'] = False
            raise Exception('given model is not linear. Lineaize the model using model.linearize() before setup().')
        return A
    
    def input_matrix(self):
        """Extracts input matix from ODE or difference equation.
        
        This method utilizes the API :py:func:`jacobian` from casadi in order to obtain the respective input matrix.

        :raises exception: given model is not linear. Lineaize the model using :py:func:`model.linearize` after :py:func:`model.setup`.

        :return: Extracted input matrix :math:`B`
        :rtype: casadi.SX

        """
        #Calculating jacobian with respect to input variables
        B = jacobian(self.model._rhs,self.model.u)
        
        #Check whether obtained matrix is constant
        if B.is_constant() == True:
            self.flags['linear'] = True
        elif B.is_constant() == False:
            self.flags['linear'] = False
            raise Exception('given model is not linear. Lineaize the model using model.linearize() after model.setup().')
        return B
        
    def discrete_gain(self,A,B):
        """Computes discrete gain. 
        
        This method computes both finite discrete gain and infinite discrete gain depending on the availability 
        of prediction horizon. The gain computed using explicit solution for both finite time and infinite time.
        
        For finite time:
            
            .. math::
                \\pi(N) &= P_f\\\\
                K(k) & = -(B'\\pi(k+1)B)^{-1}B'\\pi(k+1)A\\\\
                \\pi(k) & = Q+A'\\pi(k+1)A-A'\\pi(k+1)B(B'\\pi(k+1)B+R)^{-1}B'\\pi(k+1)A
       
        For infinite time:
            
            .. math::
                K & = -(B'PB+P)^{-1}B'PA\\\\
                P & = Q+A'PA-A'PB(R+B'PB)^{-1}B'PA\\\\
        
        For example:
            
            ::
                
                K = lqr.discrete_gain(A,B)
                                  
        :param A: State matrix - constant matrix with no variables
        :type A: numpy.ndarray

        :param B: Input matrix - constant matrix with no variables
        :type B: numpy.ndarray                 
        
        :return: Gain matrix :math:`K`
        :rtype: numpy.ndarray

        """
        #Verifying the availability of cost matrices
        assert np.shape(self.Q) == (self.model.n_x,self.model.n_x) and np.shape(self.R) == (self.model.n_u,self.model.n_u) , 'Enter tuning parameter Q and R for the lqr problem using set_objective() function.'
        assert self.model_type == 'discrete', 'convert the model from continous to discrete using model_type_conversion() function.'
        
        #calculating finite horizon gain
        if self.n_horizon !=0:
            assert self.P.size != 0, 'Terminal cost is required to calculate gain. Enter the required value using set_objective() function.'
            temp_p = self.P
            for k in range(self.n_horizon):
                 K = -np.linalg.inv(np.transpose(B)@temp_p@B+self.R)@np.transpose(B)@temp_p@A
                 temp_pi = self.Q+np.transpose(A)@temp_p@A-np.transpose(A)@temp_p@B@np.linalg.inv(np.transpose(B)@temp_p@B+self.R)@np.transpose(B)@temp_p@A
                 temp_p = temp_pi
            return K
        
        #Calculating infinite horizon gain
        elif self.n_horizon == 0:
            pi_discrete = solve_discrete_are(A,B, self.Q, self.R)
            K = -np.linalg.inv(np.transpose(B)@pi_discrete@B+self.R)@np.transpose(B)@pi_discrete@A
            return K
    
    def input_rate_penalization(self):
        """Computes lqr gain for the input rate penalization mode.
        
        This method modifies the state matrix and input matrix according to the input rate penalization method. Due to this objective function also gets modified.
        The input rate penalization formulation is given as:
            
            .. math::
                x(k+1) = \\tilde{A} x(k) + \\tilde{B}\\Delta u(k)\\\\
                
                \\text{where} 
                \\tilde{A} = \\begin{bmatrix} 
                                A & B \\\\
                                0 & I \\end{bmatrix},
                \\tilde{B} = \\begin{bmatrix} B \\\\
                             I \\end{bmatrix}
                            
        
        The above formulation is with respect to discrete time system. After formulating the objective, discrete gain is calculated
        using :py:func:`discrete_gain`
        
        :return: Gain matrix :math:`K`
        :rtype: numpy.ndarray
        

        """
        
        #verifying the given model is daemodel
        assert self.model.flags['dae2odemodel']==False, 'Already performing input rate penalization without the use of this function'
        
        #Verifying input cost matrix for input rate penalization
        assert self.R_delu.size != 0 , 'set R_delu parameter using set_param() fun.'
        
        #Modifying A and B matrix for input rate penalization
        identity_u = np.identity(np.shape(self.B)[1])
        zeros_A = np.zeros((np.shape(self.B)[1],np.shape(self.A)[1]))
        self.A_new = np.block([[self.A,self.B],[zeros_A,identity_u]])
        self.B_new = np.block([[self.B],[identity_u]])
        zeros_Q = np.zeros((np.shape(self.Q)[0],np.shape(self.R)[1]))
        zeros_Ru = np.zeros((np.shape(self.R)[0],np.shape(self.Q)[1]))
        
        #Modifying Q and R matrix for input rate penalization
        self.Q = np.block([[self.Q,zeros_Q],[zeros_Ru,self.R]])
        self.P = np.block([[self.P,zeros_Q],[zeros_Ru,self.R]])
        self.R = self.R_delu
        
        #Computing gain matrix
        K = self.discrete_gain(self.A_new, self.B_new)
        return K
 
    
    def set_param(self,**kwargs):
        """Set the parameters of the :py:class:`LQR` class. Parameters must be passed as pairs of valid keywords and respective argument.
        For example:

        ::

            lqr.set_param(n_horizon = 20)

        It is also possible and convenient to pass a dictionary with multiple parameters simultaneously as shown in the following example:

        ::

            setup_lqr = {
                'n_horizon': 20,
                't_sample': 0.5,
            }
            lqr.set_param(**setup_mpc)
        
        This makes use of thy python "unpack" operator. See `more details here`_.

        .. _`more details here`: https://codeyarns.github.io/tech/2012-04-25-unpack-operator-in-python.html

        .. note:: The only required parameters  are ``n_horizon``. All other parameters are optional.

        .. note:: :py:func:`set_param` can be called multiple times. Previously passed arguments are overwritten by successive calls.

        The following parameters are available:
            
        :param n_horizon: Prediction horizon of the optimal control problem. Parameter must be set by user.
        :type n_horizon: int

        :param t_sample: Sampling time for converting continuous time system to discrete time system.
        :type t_sample: float
        
        :param mode: mode for operating LQR
        :type mode: String
        
        :param conv_method: Method for converting continuous time to discrete time system
        :type conv_method: String
        
        """
        for key, value in kwargs.items():
            if not (key in self.data_fields):
                print('Warning: Key {} does not exist for LQR.'.format(key))
            else:
                setattr(self, key, value)
                        
    def steady_state(self):
        """Calculates steady states for the given input or states.
        
        This method calculates steady states for the given steady state input and vice versa.
        The mathematical formulation can be described as 
            
            .. math::
                x_{ss} = (I-A)^{-1}Bu_{ss}\\\\
                
               or\\\\
                
                u_{ss} = B^{-1}(I-A)x_{ss}
        
        :return: Steady state
        :rtype: numpy.ndarray
        
        :return: Steady state input
        :rtype: numpy.ndarray

        """
        #Check whether the model is linear and setup
        assert self.flags['setup'] == True, 'LQR is not setup. Please run setup() fun to calculate steady state.'
        assert self.flags['linear'] == True, 'Provide a linear model by executing linearize() function'
        I = np.identity(np.shape(self.A)[0])
        
        #Calculation of steady state
        if self.xss.size == 0:
            self.xss = np.linalg.inv(I-self.A)@self.B@self.uss
            return self.xss
        elif self.uss.size == 0 and np.shape(self.B)[0] != np.shape(self.B)[1]:
            self.uss = np.linalg.pinv(self.B)@(I-self.A)@self.xss
            return self.uss
        elif self.uss.size == 0 and np.shape(self.B)[0] == np.shape(self.B)[1]:
            self.uss = np.linalg.inv(self.B)@(I-self.A)@self.xss
            return self.uss   
                
    def make_step(self,x0,z0=None):
        """Main method of the class during runtime. This method is called at each timestep
        and returns the control input for the current initial state.
        
        .. note::
            
            :math:`z0` should be passed when the original model of the system contains algebraic variables
            
        .. note::
            
            LQR will always run in the set point tracking mode irrespective of the set point is not specified
            
        .. note::
            
            LQR cannot be made to execute in the input rate penalization mode if the model is converted from DAE to ODE system.
            Because the converted model itself is in input rate penalization mode.

        :param x0: Current state of the system.
        :type x0: numpy.ndarray
        
        :param z0: Current algebraic state of the system (optional).
        :type z0: numpy.ndarray

        :return: u0
        :rtype: numpy.ndarray
        """
        #verify setup of lqr is done
        assert self.flags['setup'] == True, 'LQR is not setup. run setup() function.'
        
        #setting setpoints
        if self.xss is None and self.uss is None:
            self.set_point()
        
        #Initializing u0
        if self.u0.size == 0:
            self.u0 = np.zeros((np.shape(self.B)[1],1))
        
        #Calculate u in set point tracking mode
        if self.mode == "setPointTrack" and self.model.flags['dae2odemodel'] == False:
            if self.xss.size != 0 and self.uss.size != 0:
                self.u0 = self.K@(x0-self.xss)+self.uss
            return self.u0
        
        #Calculate u in input rate penalization mode
        elif self.mode == "inputRatePenalization" and self.model.flags['dae2odemodel']==False:
            if np.shape(self.K)[1]==np.shape(x0)[0]:
                self.u0 = self.K@(x0-self.xss)+self.uss
                self.u0 = self.u0+x0[-self.model.n_u:]
            elif np.shape(self.K)[1]!=np.shape(x0)[0] and np.shape(self.K)[1]== np.shape(np.block([[x0],[self.u0]]))[0]:
                x0_new = np.block([[x0],[self.u0]])
                self.u0 = self.K@(x0_new)
                self.u0 = self.u0+x0_new[-self.model.n_u:]
            return self.u0
        
        #Calculate u for converted ode model
        elif self.model.flags['dae2odemodel']==True:
            assert z0 != None,'Please pass initial value for algebraic variables'
            x0_new = np.block([[x0],[self.u0],[z0]])
            self.u0 = self.K@(x0_new-self.xss)+self.uss
            if np.shape(x0)[0]==np.shape(x0)[0]+np.shape(self.u0)[0]-1:
                self.u0 = self.u0+x0_new[np.shape(x0)[0]]
            else:
                self.u0 = self.u0+x0_new[np.shape(x0)[0]:np.shape(x0)[0]+np.shape(self.u0)[0]-1]
            return self.u0
        
    def convertSX_to_array(self,A,B):
        """Converts casadi symbolic variable to numpy array.
        
        The following method is initially convets casADi SX or MX variable to casadi DM variable.
        Then using casadi API, casadi variable is converted to numpy array.
        For example:
            
        ::
            
            [A_new,B_new] = lqr.convertSX_to_array(A,B)
        
        :param A: State matrix - constant matrix with no variables
        :type A: Casadi SX or MX

        :param B: Input matrix - constant matrix with no variables
        :type B: Casadi SX or MX 

        :return: State matrix :math:`A`
        :rtype: numpy.ndarray

        :return: Input matrix :math:`B`
        :rtype: numpy.ndarray 
        """
        
        #Creating casadi function to conver to DM type
        y1 = A
        y2 = B
        A_new = Function('A_new',[],[y1])
        B_new = Function('B_new',[],[y2])
        A = A_new()
        B = B_new()
        
        #Converting from DM to numpy array
        arr_A = A['o0'].full()
        arr_B = B['o0'].full()
        return arr_A,arr_B
    
    def dae_model_gain(self,A,B):
        """Computes discrete gain for the model converted with the help of :py:func:`model.dae_to_ode_model`.
        
        This method computes gain for the converted ode model in which total number of states differ from the original model.
        Therefore this method computes the :math:`Q` and :math:`R` for the converted ode model. The parameter for objective function 
        is set using :py:func:`set_objective`. The cost matrices are modified as follows
        
        .. math::
              Q_{mod} = \\begin{bmatrix} Q & 0 & 0 \\\\ 0 & R & 0 \\\\ 0 & 0 & \\Delta Z \\end{bmatrix}
                
        .. math::
              R_{mod} = \\Delta R\\\\
        
        .. math::
            P_{mod} = \\begin{bmatrix}
                    P & 0 & 0\\\\ 0 & R & 0\\\\
                    0 & 0 & \\Delta Z
                \\end{bmatrix}
        
        Where :math:`Q` and :math:`R` are the cost matrices of converted ode model, :math:`\\Delta R` is the cost matrix for the
        penalized input rate and :math:`\\Delta Z`  is the cost matrix for the algebraic variables. All the variables can be set using :py:func:`set_objective`.
        
        After modifying the weight matrices, discrete gain is calculated using :py:func:`discrete_gain`.
        For example:
            
            ::
                
                K = lqr.dae_model_gain(A,B)
        
        :param A: State matrix
        :type A: numpy.ndarray
        
        :param B: Input matrix
        :type B: numpy.ndarray
        
        :return: Gain matrix - :math:`K`
        :rtype: numpy.ndarray

        """
        #Verify the size of Q
        assert not self.Q is None, "run this function after setting objective using set_objective()"
        if np.shape(self.Q)==(self.model.n_x,self.model.n_x) and np.shape(self.R)==(self.model.n_u,self.model.n_u):
            K = self.discrete_gain(A, B)
        else:
            #Compute new Q and R
            zeros_Q_u_col = np.zeros((np.shape(self.Q)[0],np.shape(self.R)[0]+np.shape(self.delZ)[0]))
            zeros_Q_z_col = np.zeros((np.shape(self.R)[0],np.shape(self.delZ)[0]))
            zeros_Q_u_row = np.zeros((np.shape(self.R)[0],np.shape(self.Q)[0]))
            zeros_Q_z_row = np.zeros((np.shape(self.delZ)[0],np.shape(self.Q)[0]+np.shape(self.R)[0]))
            self.Q = np.block([[self.Q,zeros_Q_u_col],
                               [zeros_Q_u_row, self.R, zeros_Q_z_col],
                               [zeros_Q_z_row,self.delZ]])
            self.R = self.Rdelu
            self.P = np.block([[self.P,zeros_Q_u_col],
                               [zeros_Q_u_row, self.R, zeros_Q_z_col],
                               [zeros_Q_z_row,self.delZ]])
            #Compute discrete gain
            K = self.discrete_gain(A, B)
        return K
        
    def set_objective(self, Q = None, R = None, P = None, Rdelu = None, delZ = None):
        """Sets the cost matrix for the Optimal Control Problem.
        
        This method sets the inputs, states and algebraic states cost matrices for the given problem.
        
        .. note::
            For the problem to be solved in input rate penalization mode, :math:`Q`, :math:`R` and :math:`Rdelu` should be set.
        
        .. note::
            For a problem with dae to ode converted model, :math:`Q`, :math:`R`, :math:`Rdelu` and :math:`delZ` should be set.
            
        For example:
            
            ::
                
                # Values used are to show how to use this function.
                # For ODE models
                lqr.set_objective(Q = np.identity(2), R = np.identity(2))
                
                # For ODE models with input rate penalization
                lqr.set_objective(Q = np.identity(2), R = 5*np.identity(2), Rdelu = np.identity(2))
                
                # For DAE converted to ODE models
                lqr.set_objective(Q = np.identity(2), R = 5*np.identity(2), Rdelu = np.identity(2), delZ = np.identity(1))
                
        
        :param Q: State cost matrix
        :type Q: numpy.ndarray
        
        :param R: Input cost matrix
        :type R: numpy.ndarray
        
        :param Rdelu: Input rate cost matrix
        :type Rdelu: numpy.ndarray
        
        :param delZ: Algebraic state cost matrix
        :type delZ: numpy.ndarray
        
        :raises exception: Please set input cost matrix for input rate penalization/daemodel using :py:func:`set_objective`.
        :raises exception: Please set cost matrix for algebraic variables using :py:func:`set_objective` for evaluating daemodel
        :raises exception: Q matrix must be of type class numpy.ndarray
        :raises exception: R matrix must be of type class numpy.ndarray
        :raises exception: P matrix must be of type class numpy.ndarray

        .. warning::
            Q, R, P is chosen as matrix of zeros since it is not passed explicitly.
            P is not given explicitly. Q is chosen as P for calculating finite discrete gain

        """
        
        #Verify the setup is not complete
        assert self.flags['setup'] == False, 'Objective can not be set after LQR is setup'
        
        #Set Q, R, P
        if Q is None:
            self.Q = np.zeros((self.model.n_x,self.model.n_x))
            warnings.warn('Q is chosen as matrix of zeros since Q is not passed explicitly.')
        else:
            self.Q = Q
        if R is None:
            self.R = np.zeros((self.model.n_u,self.model.n_u))
            warnings.warn('R is chosen as matrix of zeros.')
        else:
            self.R = R   
        if P is None and self.n_horizon != 0:
            self.P = Q
            warnings.warn('P is not given explicitly. Q is chosen as P for calculating finite discrete gain')
        else:
            self.P = P

        #Set delRu for input rate penalization or converted ode model
        if (self.mode == 'inputRatePenalization' or self.model.flags['dae2odemodel'] == True) and Rdelu != None:
            self.Rdelu = Rdelu
        elif (self.mode == 'inputRatePenalization' or self.model.flags['dae2odemodel']==True) and Rdelu == None:
            raise Exception('Please set input cost matrix for input rate penalization/daemodel using set_objective()')
        
        #Set delZ for converted ode model
        if self.model.flags['dae2odemodel'] == True and delZ != None and np.shape(self.Q) != (self.model.n_x,self.model.n_x):
            self.delZ = delZ
        elif self.model.flags['dae2odemodel'] == True and delZ == None:
            raise Exception('Please set cost matrix for algebraic variables using set_objective() for evaluating daemodel')
        
        #Verify shape of Q,R,P
        if self.model.flags['dae2odemodel']==False:    
            assert self.Q.shape == (self.model.n_x,self.model.n_x), 'Q must have shape = {}. You have {}'.format((self.model.n_x,self.model.n_x),self.Q.shape)
            assert self.R.shape == (self.model.n_u,self.model.n_u), 'R must have shape = {}. You have {}'.format((self.model.n_u,self.model.n_u),self.R.shape)
        if isinstance(self.Q, (casadi.DM, casadi.SX, casadi.MX)):
            raise Exception('Q matrix must be of type class numpy.ndarray')
        if isinstance(self.R, (casadi.DM, casadi.SX, casadi.MX)):
            raise Exception('R matrix must be of type class numpy.ndarray')
        if self.n_horizon != 0 and isinstance(self.P, (casadi.DM, casadi.SX, casadi.MX)):
            raise Exception('P matrix must be of type class numpy.ndarray')
        if self.n_horizon != 0:
            assert self.P.shape == self.Q.shape, 'P must have same shape as Q. You have {}'.format(P.shape)

    def set_point(self,xss = None,uss = None,zss = None):   
        """Sets setpoints for states and inputs.
        
        This method can be used to set setpoints at each time step. It can be called inside simulation loop to change the set point dynamically.
        
        .. note::
            If setpoints is not specifically mentioned it will be set to zero (default).
        
        For example:
            
            ::
                
                # For ODE models
                lqr.set_point(xss = np.array([[10],[15]]) ,uss = np.array([[2],[3]]))
                
                # For DAE to ODE converted models
                lqr.set_point(xss = np.array([[10],[15]]) ,uss = np.array([[2],[3]]), zss = np.array([[3]]))

        :param xss: set point for states of the system(optional)
        :type xss: numpy.ndarray
        
        :param uss: set point for input of the system(optional)
        :type uss: numpy.ndarray
        
        :param zss: set point for algebraic states of the system(optional)
        :type zss: numpy.ndarray

        """
        assert self.flags['setup'] == True, 'LQR is not setup. Run setup() function.'
        if xss is None:
            self.xss = np.zeros((self.model.n_x,1))
        else:
            self.xss = xss
        
        if uss is None:
            self.uss = np.zeros((self.model.n_u,1))
        else:
            self.uss = uss
        
        if self.model.flags['dae2odemodel'] == True:
            if zss is None:
                self.zss = np.zeros((np.shape(self.delZ)[0],1))
            else:
                self.zss = zss
                self.xss = np.block([[self.xss],[self.uss],[self.zss]])
            assert self.zss.shape == (np.shape(self.delZ)[0],1), 'xss must be of shape {}. You have {}'.format((np.shape(self.delZ)[0],1),self.zss.shape)
            
        assert self.xss.shape == (self.model.n_x,1), 'xss must be of shape {}. You have {}'.format((self.model.n_x,1),self.xss.shape)
        assert self.uss.shape == (self.model.n_u,1), 'uss must be of shape {}. You have {}'.format((self.model.n_u,1),self.uss.shape)

    def setup(self):
        """Prepares lqr for execution.
        This method initializes and make sure that all the necessary parameters required to run the lqr are available.
        
        :raises exception: mode must be setPointTrack, inputRatePenalization, None. you have {string value}

        """
        
        A = self.state_matrix()
        B = self.input_matrix()
        [self.A,self.B] = self.convertSX_to_array(A, B)
        if self.model_type == 'continuous':
            [self.A,self.B] = self.continuous_to_discrete_time()
        assert self.flags['linear']== True, 'Model is not linear'
        if self.n_horizon == 0:
            warnings.warn('discrete infinite horizon gain will be computed since prediction horizon is set to default value 0')
        if self.mode in ['setPointTrack',None] and self.model.flags['dae2odemodel'] == False and self.model.n_z==0:
            self.K = self.discrete_gain(self.A,self.B)
        elif self.mode == 'inputRatePenalization' and self.model.flags['dae2odemodel'] == False and self.model.n_z==0:
            self.K = self.input_rate_penalization()
        elif self.model.flags['dae2odemodel'] == True:
            self.K = self.dae_model_gain(self.A, self.B)
        elif not self.mode in ['setPointTrack','inputRatePenalization']:
            raise Exception('mode must be setPointTrack, inputRatePenalization, None. you have {}'.format(self.method))
        elif self.model.flags['dae2odemodel'] == False and self.model.n_z !=0:
            raise Exception('You model contains algebraic states. Please convert the dae model to ode model using model.dae_to_ode_model().')
        self.flags['setup'] = True

class MPC(do_mpc.optimizer.Optimizer, do_mpc.model.IteratedVariables):
    """Model predictive controller.

    For general information on model predictive control, please read our `background article`_.

    .. _`background article`: ../theory_mpc.html

    The MPC controller extends the :py:class:`do_mpc.optimizer.Optimizer` base class
    (which is also used for the :py:class:`do_mpc.estimator.MHE` estimator).

    Use this class to configure and run the MPC controller
    based on a previously configured :py:class:`do_mpc.model.Model` instance.

    **Configuration and setup:**

    Configuring and setting up the MPC controller involves the following steps:

    1. Use :py:func:`set_param` to configure the :py:class:`MPC` instance.

    2. Set the objective of the control problem with :py:func:`set_objective` and :py:func:`set_rterm`

    3. Set upper and lower bounds with :py:attr:`bounds` (optional).

    4. Set further (non-linear) constraints with :py:func:`set_nl_cons` (optional).

    5. Use the low-level API (:py:func:`get_p_template` and :py:func:`set_p_fun`) or high level API (:py:func:`set_uncertainty_values`) to create scenarios for robust MPC (optional).

    6. Use :py:meth:`get_tvp_template` and :py:meth:`set_tvp_fun` to create a method to obtain new time-varying parameters at each iteration.

    7. To finalize the class configuration there are two routes. The default approach is to call :py:meth:`setup`. For deep customization use the combination of :py:meth:`prepare_nlp` and :py:meth:`create_nlp`. See graph below for an illustration of the process.

    .. graphviz::
        :name: route_to_setup
        :caption: Route to setting up the MPC class.
        :align: center

        digraph G {
            graph [fontname = "helvetica"];
            rankdir=LR;

            subgraph cluster_main {
                node [fontname = "helvetica", shape=box, fontcolor="#404040", color="#707070"];
                edge [fontname = "helvetica", color="#707070"];

                start [label="Two ways to setup"];
                setup [label="setup", href="../api/do_mpc.controller.MPC.setup.html", target="_top", fontname = "Consolas"];
                create_nlp [label="create_nlp", href="../api/do_mpc.controller.MPC.create_nlp.html", target="_top", fontname = "Consolas"];
                process [label="Modify NLP"];
                prepare_nlp [label="prepare_nlp", href="../api/do_mpc.controller.MPC.prepare_nlp.html", target="_top", fontname = "Consolas"];
                finish [label="Configured MPC class"]
                start -> setup, prepare_nlp;
                prepare_nlp -> process;
                process -> create_nlp;
                setup, create_nlp -> finish;
                color=none;
            }

            subgraph cluster_modification {
                rankdir=TB;
                node [fontname = "helvetica", shape=box, fontcolor="#404040", color="#707070"];
                edge [fontname = "helvetica", color="#707070"];
                opt_x [label="opt_x", href="../api/do_mpc.controller.MPC.opt_x.html", target="_top", fontname = "Consolas"];
                opt_p [label="opt_p", href="../api/do_mpc.controller.MPC.opt_p.html", target="_top", fontname = "Consolas"];
                nlp_cons [label="nlp_cons", href="../api/do_mpc.controller.MPC.nlp_cons.html", target="_top", fontname = "Consolas"];
                nlp_obj [label="nlp_obj", href="../api/do_mpc.controller.MPC.nlp_obj.html", target="_top", fontname = "Consolas"];

                opt_x -> nlp_cons, nlp_obj;
                opt_p -> nlp_cons, nlp_obj;

                label = "Attributes to modify the NLP.";
		        color=black;
            }

            nlp_cons -> process;
            nlp_obj -> process;
        }

    .. warning::

        Before running the controller, make sure to supply a valid initial guess for all optimized variables (states, algebraic states and inputs).
        Simply set the initial values of :py:attr:`x0`, :py:attr:`z0` and :py:attr:`u0` and then call :py:func:`set_initial_guess`.

        To take full control over the initial guess, modify the values of :py:attr:`opt_x_num`.

    During runtime call :py:func:`make_step` with the current state :math:`x` to obtain the optimal control input :math:`u`.

    """

    def __init__(self, model):

        self.model = model

        assert model.flags['setup'] == True, 'Model for MPC was not setup. After the complete model creation call model.setup().'
        self.data = do_mpc.data.MPCData(self.model)
        self.data.dtype = 'MPC'

        # Initialize parent class:
        do_mpc.model.IteratedVariables.__init__(self)
        do_mpc.optimizer.Optimizer.__init__(self)

        # Initialize further structures specific to the MPC optimization problem.
        # This returns an identical numerical structure with all values set to the passed value.
        self._x_terminal_lb = model._x(-np.inf)
        self._x_terminal_ub = model._x(np.inf)

        self.rterm_factor = self.model._u(0.0)

        # Initialize structure to hold the optimial solution and initial guess:
        self._opt_x_num = None
        # Initialize structure to hold the parameters for the optimization problem:
        self._opt_p_num = None

        # Parameters that can be set for the optimizer:
        self.data_fields = [
            'n_horizon',
            'n_robust',
            'open_loop',
            't_step',
            'use_terminal_bounds',
            'state_discretization',
            'collocation_type',
            'collocation_deg',
            'collocation_ni',
            'nl_cons_check_colloc_points',
            'nl_cons_single_slack',
            'cons_check_colloc_points',
            'store_full_solution',
            'store_lagr_multiplier',
            'store_solver_stats',
            'nlpsol_opts'
        ]

        # Default Parameters (param. details in set_param method):
        self.n_robust = 0
        self.open_loop = False
        self.use_terminal_bounds = True
        self.state_discretization = 'collocation'
        self.collocation_type = 'radau'
        self.collocation_deg = 2
        self.collocation_ni = 1
        self.nl_cons_check_colloc_points = False
        self.nl_cons_single_slack = False
        self.cons_check_colloc_points = True
        self.store_full_solution = False
        self.store_lagr_multiplier = True
        self.store_solver_stats = [
            'success',
            't_wall_total',
        ]
        self.nlpsol_opts = {} # Will update default options with this dict.

        # Flags are checked when calling .setup.
        self.flags.update({
            'set_objective': False,
            'set_rterm': False,
            'set_tvp_fun': False,
            'set_p_fun': False,
            'set_initial_guess': False,
        })

    @property
    def opt_x_num(self):
        """Full MPC solution and initial guess.

        This is the core attribute of the MPC class.
        It is used as the initial guess when solving the optimization problem
        and then overwritten with the current solution.

        The attribute is a CasADi numeric structure with nested power indices.
        It can be indexed as follows:

        ::

            # dynamic states:
            opt_x_num['_x', time_step, scenario, collocation_point, _x_name]
            # algebraic states:
            opt_x_num['_z', time_step, scenario, collocation_point, _z_name]
            # inputs:
            opt_x_num['_u', time_step, scenario, _u_name]
            # slack variables for soft constraints:
            opt_x_num['_eps', time_step, scenario, _nl_cons_name]

        The names refer to those given in the :py:class:`do_mpc.model.Model` configuration.
        Further indices are possible, if the variables are itself vectors or matrices.

        The attribute can be used **to manually set a custom initial guess or for debugging purposes**.

        **How to query?**

        Querying the structure is more complicated than it seems at first look because of the scenario-tree used
        for robust MPC. To obtain all collocation points for the finite element at time-step :math:`k` and scenario :math:`b` use:

        ::

            horzcat(*[mpc.opt_x_num['_x',k,b,-1]]+mpc.opt_x_num['_x',k+1,b,:-1])

        Due to the multi-stage formulation at any given time :math:`k` we can have multiple future scenarios.
        However, there is only exactly one scenario that lead to the current node in the tree.
        Thus the collocation points associated to the finite element :math:`k` lie in the past.

        The concept is illustrated in the figure below:

        .. figure:: ../static/collocation_points_scenarios.svg

        .. note::

            The attribute ``opt_x_num`` carries the scaled values of all variables. See ``opt_x_num_unscaled``
            for the unscaled values (these are not used as the initial guess).

        .. warning::

            Do not tweak or overwrite this attribute unless you known what you are doing.

        .. note::

            The attribute is populated when calling :py:func:`setup`
        """
        return self._opt_x_num

    @opt_x_num.setter
    def opt_x_num(self, val):
        self._opt_x_num = val

    @property
    def opt_p_num(self):
        """Full MPC parameter vector.

        This attribute is used when calling the MPC solver to pass all required parameters,
        including

        * initial state

        * uncertain scenario parameters

        * time-varying parameters

        * previous input sequence

        **do-mpc** handles setting these parameters automatically in the :py:func:`make_step`
        method. However, you can set these values manually and directly call :py:func:`solve`.

        The attribute is a CasADi numeric structure with nested power indices.
        It can be indexed as follows:

        ::

            # initial state:
            opt_p_num['_x0', _x_name]
            # uncertain scenario parameters
            opt_p_num['_p', scenario, _p_name]
            # time-varying parameters:
            opt_p_num['_tvp', time_step, _tvp_name]
            # input at time k-1:
            opt_p_num['_u_prev', time_step, scenario]

        The names refer to those given in the :py:class:`do_mpc.model.Model` configuration.
        Further indices are possible, if the variables are itself vectors or matrices.

        .. warning::

            Do not tweak or overwrite this attribute unless you known what you are doing.

        .. note::

            The attribute is populated when calling :py:func:`setup`

        """
        return self._opt_p_num

    @opt_p_num.setter
    def opt_p_num(self, val):
        self._opt_p_num = val

    @property
    def opt_x(self):
        """Full structure of (symbolic) MPC optimization variables.

        The attribute is a CasADi symbolic structure with nested power indices.
        It can be indexed as follows:

        ::

            # dynamic states:
            opt_x['_x', time_step, scenario, collocation_point, _x_name]
            # algebraic states:
            opt_x['_z', time_step, scenario, collocation_point, _z_name]
            # inputs:
            opt_x['_u', time_step, scenario, _u_name]
            # slack variables for soft constraints:
            opt_x['_eps', time_step, scenario, _nl_cons_name]

        The names refer to those given in the :py:class:`do_mpc.model.Model` configuration.
        Further indices are possible, if the variables are itself vectors or matrices.

        The attribute can be used to alter the objective function or constraints of the NLP.

        **How to query?**

        Querying the structure is more complicated than it seems at first look because of the scenario-tree used
        for robust MPC. To obtain all collocation points for the finite element at time-step :math:`k` and scenario :math:`b` use:

        ::

            horzcat(*[mpc.opt_x['_x',k,b,-1]]+mpc.opt_x['_x',k+1,b,:-1])

        Due to the multi-stage formulation at any given time :math:`k` we can have multiple future scenarios.
        However, there is only exactly one scenario that lead to the current node in the tree.
        Thus the collocation points associated to the finite element :math:`k` lie in the past.

        The concept is illustrated in the figure below:

        .. figure:: ../static/collocation_points_scenarios.svg

        .. note::

            The attribute ``opt_x`` carries the scaled values of all variables.

        .. note::

            The attribute is populated when calling :py:func:`setup` or :py:func:`prepare_nlp`
        """
        return self._opt_x

    @opt_x.setter
    def opt_x(self, val):
        self._opt_x = val

    @property
    def opt_p(self):
        """Full structure of (symbolic) MPC parameters.

        The attribute is a CasADi numeric structure with nested power indices.
        It can be indexed as follows:

        ::

            # initial state:
            opt_p['_x0', _x_name]
            # uncertain scenario parameters
            opt_p['_p', scenario, _p_name]
            # time-varying parameters:
            opt_p['_tvp', time_step, _tvp_name]
            # input at time k-1:
            opt_p['_u_prev', time_step, scenario]

        The names refer to those given in the :py:class:`do_mpc.model.Model` configuration.
        Further indices are possible, if the variables are itself vectors or matrices.

        .. warning::

            Do not tweak or overwrite this attribute unless you known what you are doing.

        .. note::

            The attribute is populated when calling :py:func:`setup` or :py:func:`prepare_nlp`

        """
        return self._opt_p

    @opt_p.setter
    def opt_p(self, val):
        self._opt_p = val


    @IndexedProperty
    def terminal_bounds(self, ind):
        """Query and set the terminal bounds for the states.
        The :py:func:`terminal_bounds` method is an indexed property, meaning
        getting and setting this property requires an index and calls this function.
        The power index (elements are seperated by comas) must contain atleast the following elements:

        ======      =================   ==========================================================
        order       index name          valid options
        ======      =================   ==========================================================
        1           bound type          ``lower`` and ``upper``
        2           variable name       Names defined in :py:class:`do_mpc.model.Model`.
        ======      =================   ==========================================================

        Further indices are possible (but not neccessary) when the referenced variable is a vector or matrix.

        **Example**:

        ::

            # Set with:
            optimizer.terminal_bounds['lower', 'phi_1'] = -2*np.pi
            optimizer.terminal_bounds['upper', 'phi_1'] = 2*np.pi

            # Query with:
            optimizer.terminal_bounds['lower', 'phi_1']

        """
        assert isinstance(ind, tuple), 'Power index must include bound_type, var_name (as a tuple).'
        assert len(ind)>=2, 'Power index must include bound_type, var_type, var_name (as a tuple).'
        bound_type = ind[0]
        var_name   = ind[1:]

        err_msg = 'Invalid power index {} for bound_type. Must be from (lower, upper).'
        assert bound_type in ('lower', 'upper'), err_msg.format(bound_type)

        if bound_type == 'lower':
            query = '{var_type}_{bound_type}'.format(var_type=var_type, bound_type='lb')
        elif bound_type == 'upper':
            query = '{var_type}_{bound_type}'.format(var_type=var_type, bound_type='ub')
        # query results string e.g. _x_lb, _x_ub, _u_lb, u_ub ....

        # Get the desired struct:
        var_struct = getattr(self, query)

        err_msg = 'Calling .bounds with {} is not valid. Possible keys are {}.'
        assert (var_name[0] if isinstance(var_name, tuple) else var_name) in var_struct.keys(), msg.format(ind, var_struct.keys())

        return var_struct[var_name]

    @terminal_bounds.setter
    def terminal_bounds(self, ind, val):
        """See Docstring for bounds getter method"""

        assert isinstance(ind, tuple), 'Power index must include bound_type, var_type, var_name (as a tuple).'
        assert len(ind)>=3, 'Power index must include bound_type, var_type, var_name (as a tuple).'
        bound_type = ind[0]
        var_type   = ind[1]
        var_name   = ind[2:]

        err_msg = 'Invalid power index {} for bound_type. Must be from (lower, upper).'
        assert bound_type in ('lower', 'upper'), err_msg.format(bound_type)
        err_msg = 'Invalid power index {} for var_type. Must be from (_x, _u, _z, _p_est).'
        assert var_type in ('_x', '_u', '_z', '_p_est'), err_msg.format(var_type)

        if bound_type == 'lower':
            query = '_x_terminal_lb'
        elif bound_type == 'upper':
            query = '_x_terminal_ub'

        # Get the desired struct:
        var_struct = getattr(self, query)

        err_msg = 'Calling .bounds with {} is not valid. Possible keys are {}.'
        assert (var_name[0] if isinstance(var_name, tuple) else var_name) in var_struct.keys(), msg.format(ind, var_struct.keys())

        # Set value on struct:
        var_struct[var_name] = val



    def set_param(self, **kwargs):
        """Set the parameters of the :py:class:`MPC` class. Parameters must be passed as pairs of valid keywords and respective argument.
        For example:

        ::

            mpc.set_param(n_horizon = 20)

        It is also possible and convenient to pass a dictionary with multiple parameters simultaneously as shown in the following example:

        ::

            setup_mpc = {
                'n_horizon': 20,
                't_step': 0.5,
            }
            mpc.set_param(**setup_mpc)

        This makes use of thy python "unpack" operator. See `more details here`_.

        .. _`more details here`: https://codeyarns.github.io/tech/2012-04-25-unpack-operator-in-python.html

        .. note:: The only required parameters  are ``n_horizon`` and ``t_step``. All other parameters are optional.

        .. note:: :py:func:`set_param` can be called multiple times. Previously passed arguments are overwritten by successive calls.

        The following parameters are available:

        :param n_horizon: Prediction horizon of the optimal control problem. Parameter must be set by user.
        :type n_horizon: int

        :param n_robust: Robust horizon for robust scenario-tree MPC, defaults to ``0``. Optimization problem grows exponentially with ``n_robust``.
        :type n_robust: int , optional

        :param open_loop: Setting for scenario-tree MPC: If the parameter is ``False``, for each timestep **AND** scenario an individual control input is computed. If set to ``True``, the same control input is used for each scenario. Defaults to False.
        :type open_loop: bool , optional

        :param t_step: Timestep of the mpc.
        :type t_step: float

        :param use_terminal_bounds: Choose if terminal bounds for the states are used. Defaults to ``True``. Set terminal bounds with :py:attr:`terminal_bounds`.
        :type use_terminal_bounds: bool

        :param state_discretization: Choose the state discretization for continuous models. Currently only ``'collocation'`` is available. Defaults to ``'collocation'``. Has no effect if model is created in ``discrete`` type.
        :type state_discretization: str

        :param collocation_type: Choose the collocation type for continuous models with collocation as state discretization. Currently only ``'radau'`` is available. Defaults to ``'radau'``.
        :type collocation_type: str

        :param collocation_deg: Choose the collocation degree for continuous models with collocation as state discretization. Defaults to ``2``.
        :type collocation_deg: int

        :param collocation_ni: For orthogonal collocation choose the number of finite elements for the states within a time-step (and during constant control input). Defaults to ``1``. Can be used to avoid high-order polynomials.
        :type collocation_ni: int

        :param nl_cons_check_colloc_points: For orthogonal collocation choose whether the nonlinear bounds set with :py:func:`set_nl_cons` are evaluated once per finite Element or for each collocation point. Defaults to ``False`` (once per collocation point).
        :type nl_cons_check_colloc_points: bool

        :param nl_cons_single_slack: If ``True``, soft-constraints set with :py:func:`set_nl_cons` introduce only a single slack variable for the entire horizon. Defaults to ``False``.
        :type nl_cons_single_slack: bool

        :param cons_check_colloc_points: For orthogonal collocation choose whether the linear bounds set with :py:attr:`bounds` are evaluated once per finite Element or for each collocation point. Defaults to ``True`` (for all collocation points).
        :type cons_check_colloc_points: bool

        :param store_full_solution: Choose whether to store the full solution of the optimization problem. This is required for animating the predictions in post processing. However, it drastically increases the required storage. Defaults to False.
        :type store_full_solution: bool

        :param store_lagr_multiplier: Choose whether to store the lagrange multipliers of the optimization problem. Increases the required storage. Defaults to ``True``.
        :type store_lagr_multiplier: bool

        :param store_solver_stats: Choose which solver statistics to store. Must be a list of valid statistics. Defaults to ``['success','t_wall_total']``.
        :type store_solver_stats: list

        :param nlpsol_opts: Dictionary with options for the CasADi solver call ``nlpsol`` with plugin ``ipopt``. All options are listed `here <http://casadi.sourceforge.net/api/internal/d4/d89/group__nlpsol.html>`_.
        :type store_solver_stats: dict

        .. note:: We highly suggest to change the linear solver for IPOPT from `mumps` to `MA27`. In many cases this will drastically boost the speed of **do-mpc**. Change the linear solver with:

            ::

                MPC.set_param(nlpsol_opts = {'ipopt.linear_solver': 'MA27'})
        .. note:: To suppress the output of IPOPT, please use:

            ::

                suppress_ipopt = {'ipopt.print_level':0, 'ipopt.sb': 'yes', 'print_time':0}
                MPC.set_param(nlpsol_opts = suppress_ipopt)
        """
        assert self.flags['setup'] == False, 'Setting parameters after setup is prohibited.'

        for key, value in kwargs.items():
            if not (key in self.data_fields):
                print('Warning: Key {} does not exist for MPC.'.format(key))
            else:
                setattr(self, key, value)


    def set_objective(self, mterm=None, lterm=None):
        """Sets the objective of the optimal control problem (OCP). We introduce the following cost function:

        .. math::
           J(x,u,z) =  \\sum_{k=0}^{N}\\left(\\underbrace{l(x_k,z_k,u_k,p_k,p_{\\text{tv},k})}_{\\text{lagrange term}}
           + \\underbrace{\\Delta u_k^T R \\Delta u_k}_{\\text{r-term}}\\right)
           + \\underbrace{m(x_{N+1})}_{\\text{meyer term}}

        which is applied to the discrete-time model **AND** the discretized continuous-time model.
        For discretization we use `orthogonal collocation on finite elements`_ .
        The cost function is evaluated only on the first collocation point of each interval.

        .. _`orthogonal collocation on finite elements`: ../theory_orthogonal_collocation.html

        :py:func:`set_objective` is used to set the :math:`l(x_k,z_k,u_k,p_k,p_{\\text{tv},k})` (``lterm``) and :math:`m(x_{N+1})` (``mterm``), where ``N`` is the prediction horizon.
        Please see :py:func:`set_rterm` for the penalization of the control inputs.

        :param lterm: Stage cost - **scalar** symbolic expression with respect to ``_x``, ``_u``, ``_z``, ``_tvp``, ``_p``
        :type lterm:  CasADi SX or MX
        :param mterm: Terminal cost - **scalar** symbolic expression with respect to ``_x`` and ``_p``
        :type mterm: CasADi SX or MX

        :raises assertion: mterm must have ``shape=(1,1)`` (scalar expression)
        :raises assertion: lterm must have ``shape=(1,1)`` (scalar expression)

        :return: None
        :rtype: None
        """
        assert mterm.shape == (1,1), 'mterm must have shape=(1,1). You have {}'.format(mterm.shape)
        assert lterm.shape == (1,1), 'lterm must have shape=(1,1). You have {}'.format(lterm.shape)
        assert self.flags['setup'] == False, 'Cannot call .set_objective after .setup().'

        _x, _u, _z, _tvp, _p = self.model['x','u','z','tvp','p']


        # Check if mterm is valid:
        if not isinstance(mterm, (casadi.DM, casadi.SX, casadi.MX)):
            raise Exception('mterm must be of type casadi.DM, casadi.SX or casadi.MX. You have: {}.'.format(type(mterm)))

        # Check if lterm is valid:
        if not isinstance(lterm, (casadi.DM, casadi.SX, casadi.MX)):
            raise Exception('lterm must be of type casadi.DM, casadi.SX or casadi.MX. You have: {}.'.format(type(lterm)))

        if mterm is None:
            self.mterm = DM(0)
        else:
            self.mterm = mterm
        # TODO: This function should be evaluated with scaled variables.
        self.mterm_fun = Function('mterm', [_x, _tvp, _p], [mterm])

        if lterm is None:
            self.lterm = DM(0)
        else:
            self.lterm = lterm

        self.lterm_fun = Function('lterm', [_x, _u, _z, _tvp, _p], [lterm])

        # Check if lterm and mterm use invalid variables as inputs.
        # For the check we evaluate the function with dummy inputs and expect a DM output.
        err_msg = '{} contains invalid symbolic variables as inputs. Must contain only: {}'
        try:
            self.mterm_fun(_x(0),_tvp(0),_p(0))
        except:
            raise Exception(err_msg.format('mterm','_x, _tvp, _p'))
        try:
            self.lterm(_x(0),_u(0), _z(0), _tvp(0), _p(0))
        except:
            err_msg.format('lterm', '_x, _u, _z, _tvp, _p')

        self.flags['set_objective'] = True

    def set_rterm(self, **kwargs):
        """Set the penality factor for the inputs. Call this function with keyword argument refering to the input names in
        :py:class:`model` and the penalty factor as the respective value.

        We define for :math:`i \\in \\mathbb{I}`, where :math:`\\mathbb{I}` is the set of inputs
        and all :math:`k=0,\\dots, N` where :math:`N` denotes the horizon:

        .. math::

            \\Delta u_{k,i} = u_{k,i} - u_{k-1,i}

        and add:

        .. math::

            \\sum_{k=0}^N \\sum_{i \\in \\mathbb{I}} r_{i}\\Delta u_{k,i}^2,

        the weighted squared cost to the MPC objective function.

        **Example:**

        ::

            # in model definition:
            Q_heat = model.set_variable(var_type='_u', var_name='Q_heat')
            F_flow = model.set_variable(var_type='_u', var_name='F_flow')

            ...
            # in MPC configuration:
            MPC.set_rterm(Q_heat = 10)
            MPC.set_rterm(F_flow = 10)
            # or alternatively:
            MPC.set_rterm(Q_heat = 10, F_flow = 10)

        In the above example we set :math:`r_{Q_{\\text{heat}}}=10`
        and :math:`r_{F_{\\text{flow}}}=10`.

        .. note::

            For :math:`k=0` we obtain :math:`u_{-1}` from the previous solution.

        """
        assert self.flags['setup'] == False, 'Cannot call .set_rterm after .setup().'

        self.flags['set_rterm'] = True
        for key, val in kwargs.items():
            assert key in self.model._u.keys(), 'Must pass keywords that refer to input names defined in model. Valid is: {}. You have: {}'.format(self.model._u.keys(), key)
            assert isinstance(val, (int, float, np.ndarray)), 'Value for {} must be int, float or numpy.ndarray. You have: {}'.format(key, type(val))
            self.rterm_factor[key] = val

    def get_p_template(self, n_combinations):
        """Obtain output template for :py:func:`set_p_fun`.

        Low level API method to set user defined scenarios for robust multi-stage MPC by defining an arbitrary number
        of combinations for the parameters defined in the model.
        For more details on robust multi-stage MPC please read our `background article`_.

        .. _`background article`: ../theory_mpc.html#robust-multi-stage-nmpc

        The method returns a structured object which is
        initialized with all zeros.
        Use this object to define values of the parameters for an arbitrary number of scenarios (defined by ``n_combinations``).

        This structure (with numerical values) should be used as the output of the ``p_fun`` function
        which is set to the class with :py:func:`set_p_fun`.

        Use the combination of :py:func:`get_p_template` and :py:func:`set_p_template` as a more adaptable alternative to :py:func:`set_uncertainty_values`.

        .. note::

            We advice less experienced users to use :py:func:`set_uncertainty_values` as an alterntive way to configure the
            scenario-tree for robust multi-stage MPC.

        **Example:**

        ::

            # in model definition:
            alpha = model.set_variable(var_type='_p', var_name='alpha')
            beta = model.set_variable(var_type='_p', var_name='beta')

            ...
            # in MPC configuration:
            n_combinations = 3
            p_template = MPC.get_p_template(n_combinations)
            p_template['_p',0] = np.array([1,1])
            p_template['_p',1] = np.array([0.9, 1.1])
            p_template['_p',2] = np.array([1.1, 0.9])

            def p_fun(t_now):
                return p_template

            MPC.set_p_fun(p_fun)

        Note the nominal case is now:
        alpha = 1
        beta = 1
        which is determined by the order in the arrays above (first element is nominal).

        :param n_combinations: Define the number of combinations for the uncertain parameters for robust MPC.
        :type n_combinations: int

        :return: None
        :rtype: None
        """
        self.n_combinations = n_combinations
        p_template = self.model.sv.sym_struct([
            entry('_p', repeat=n_combinations, struct=self.model._p)
        ])
        return p_template(0)

    def set_p_fun(self, p_fun):
        """Set function which returns parameters.
        The ``p_fun`` is called at each optimization step to get the current values of the (uncertain) parameters.

        This is the low-level API method to set user defined scenarios for robust multi-stage MPC by defining an arbitrary number
        of combinations for the parameters defined in the model.
        For more details on robust multi-stage MPC please read our `background article`_.

        .. _`background article`: ../theory_mpc.html#robust-multi-stage-nmpc

        The method takes as input a function, which MUST
        return a structured object, based on the defined parameters and the number of combinations.
        The defined function has time as a single input.

        Obtain this structured object first, by calling :py:func:`get_p_template`.

        Use the combination of :py:func:`get_p_template` and :py:func:`set_p_fun` as a more adaptable alternative to :py:func:`set_uncertainty_values`.

        .. note::

            We advice less experienced users to use :py:func:`set_uncertainty_values` as an alterntive way to configure the
            scenario-tree for robust multi-stage MPC.

        **Example:**

        ::

            # in model definition:
            alpha = model.set_variable(var_type='_p', var_name='alpha')
            beta = model.set_variable(var_type='_p', var_name='beta')

            ...
            # in MPC configuration:
            n_combinations = 3
            p_template = MPC.get_p_template(n_combinations)
            p_template['_p',0] = np.array([1,1])
            p_template['_p',1] = np.array([0.9, 1.1])
            p_template['_p',2] = np.array([1.1, 0.9])

            def p_fun(t_now):
                return p_template

            MPC.set_p_fun(p_fun)

        Note the nominal case is now:
        ``alpha = 1``,
        ``beta = 1``
        which is determined by the order in the arrays above (first element is nominal).

        :param p_fun: Function which returns a structure with numerical values. Must be the same structure as obtained from :py:func:`get_p_template`. Function must have a single input (time).
        :type p_fun: function

        :return: None
        :rtype: None
        """
        assert self.get_p_template(self.n_combinations).labels() == p_fun(0).labels(), 'Incorrect output of p_fun. Use get_p_template to obtain the required structure.'
        self.flags['set_p_fun'] = True
        self.p_fun = p_fun

    def set_uncertainty_values(self, **kwargs):
        """Define scenarios for the uncertain parameters.
        High-level API method to conveniently set all possible scenarios for multistage MPC.
        For more details on robust multi-stage MPC please read our `background article`_.

        .. _`background article`: ../theory_mpc.html#robust-multi-stage-nmpc

        Pass a number of keyword arguments, where each keyword refers to a user defined parameter name from the model definition.
        The value for each parameter must be an array (or list), with an arbitrary number of possible values for this parameter.
        The first element is the nominal case.

        **Example:**

        ::

                # in model definition:
                alpha = model.set_variable(var_type='_p', var_name='alpha')
                beta = model.set_variable(var_type='_p', var_name='beta')
                gamma = model.set_variable(var_type='_p', var_name='gamma')
                ...
                # in MPC configuration:
                alpha_var = np.array([1., 0.9, 1.1])
                beta_var = np.array([1., 1.05])
                MPC.set_uncertainty_values(
                    alpha = alpha_var,
                    beta = beta_var
                )

        .. note::

            Parameters that are not imporant for the MPC controller (e.g. MHE tuning matrices)
            can be ignored with the new interface (see ``gamma`` in the example above).


        Note the nominal case is now:
        ``alpha = 1``,
        ``beta = 1``
        which is determined by the order in the arrays above (first element is nominal).

        :param kwargs: Arbitrary number of keyword arguments.

        :return: None
        :rtype: None
        """

        # If uncertainty values are passed as dictionary, extract values and keys:
        if kwargs:
            assert isinstance(kwargs, dict), 'Pass keyword arguments, where each keyword refers to a user-defined parameter name.'
            names = [i for i in kwargs.keys()]
            valid_names = self.model.p.keys()
            err_msg = 'You passed keywords {}. Valid keywords are: {} (refering to user-defined parameter names).'
            assert set(names).issubset(set(valid_names)), err_msg.format(names, valid_names)
            values = kwargs.values()
        else:
            assert isinstance(uncertainty_values, (list, None)), 'uncertainty values must be of type list, you have: {}'.format(type(uncertainty_values))
            err_msg = 'Received a list of {} elements. You have defined {} parameters in your model.'
            assert len(uncertainty_values) == self.model.n_p, err_msg.format(len(uncertainty_values), self.model.n_p)
            values = uncertainty_values


        p_scenario = list(itertools.product(*values))
        n_combinations = len(p_scenario)
        p_template = self.get_p_template(n_combinations)

        if kwargs:
            # Dict case (only parameters with name are set):
            p_template['_p', :, names] = p_scenario
        else:
            # List case (assume ALL parameters are given ...)
            p_template['_p', :] = p_scenario

        def p_fun(t_now):
            return p_template

        self.set_p_fun(p_fun)

    def _check_validity(self):
        """Private method to be called in :py:func:`setup`. Checks if the configuration is valid and
        if the optimization problem can be constructed.
        Furthermore, default values are set if they were not configured by the user (if possible).
        Specifically, we set dummy values for the ``tvp_fun`` and ``p_fun`` if they are not present in the model.
        """
        # Objective mus be defined.
        if self.flags['set_objective'] == False:
            raise Exception('Objective is undefined. Please call .set_objective() prior to .setup().')
        # rterm should have been set (throw warning if not)
        if self.flags['set_rterm'] == False:
            warnings.warn('rterm was not set and defaults to zero. Changes in the control inputs are not penalized. Can lead to oscillatory behavior.')
            time.sleep(2)
        # tvp_fun must be set, if tvp are defined in model.
        if self.flags['set_tvp_fun'] == False and self.model._tvp.size > 0:
            raise Exception('You have not supplied a function to obtain the time-varying parameters defined in model. Use .set_tvp_fun() prior to setup.')
        # p_fun must be set, if p are defined in model.
        if self.flags['set_p_fun'] == False and self.model._p.size > 0:
            raise Exception('You have not supplied a function to obtain the parameters defined in model. Use .set_p_fun() (low-level API) or .set_uncertainty_values() (high-level API) prior to setup.')

        if np.any(self.rterm_factor.cat.full() < 0):
            warnings.warn('You have selected negative values for the rterm penalizing changes in the control input.')
            time.sleep(2)

        # Lower bounds should be lower than upper bounds:
        for lb, ub in zip([self._x_lb, self._u_lb, self._z_lb], [self._x_ub, self._u_ub, self._z_ub]):
            bound_check = lb.cat > ub.cat
            bound_fail = [label_i for i,label_i in enumerate(lb.labels()) if bound_check[i]]
            if np.any(bound_check):
                raise Exception('Your bounds are inconsistent. For {} you have lower bound > upper bound.'.format(bound_fail))

        # Are terminal bounds for the states set? If not use default values (unless MPC is setup to not use terminal bounds)
        if np.all(self._x_terminal_ub.cat == np.inf) and self.use_terminal_bounds:
            self._x_terminal_ub = self._x_ub
        if np.all(self._x_terminal_lb.cat == -np.inf) and self.use_terminal_bounds:
            self._x_terminal_lb = self._x_lb

        # Set dummy functions for tvp and p in case these parameters are unused.
        if 'tvp_fun' not in self.__dict__:
            _tvp = self.get_tvp_template()

            def tvp_fun(t): return _tvp
            self.set_tvp_fun(tvp_fun)

        if 'p_fun' not in self.__dict__:
            _p = self.get_p_template(1)

            def p_fun(t): return _p
            self.set_p_fun(p_fun)

    def setup(self):
        """Setup the MPC class.
        Internally, this method will create the MPC optimization problem under consideration
        of the supplied dynamic model and the given :py:class:`MPC` class instance configuration.

        The :py:func:`setup` method can be called again after changing the configuration
        (e.g. adapting bounds) and will simply overwrite the previous optimization problem.

        .. note::

            After this call, the :py:func:`solve` and :py:func:`make_step` method is applicable.

        .. warning::

            The :py:func:`setup` method may take a while depending on the size of your MPC problem.
            Note that especially for robust multi-stage MPC with a long robust horizon and many
            possible combinations of the uncertain parameters very large problems will arise.

            For more details on robust multi-stage MPC please read our `background article`_.

        .. _`background article`: ../theory_mpc.html#robust-multi-stage-nmpc

        """

        self.prepare_nlp()
        self.create_nlp()



    def set_initial_guess(self):
        """Initial guess for optimization variables.
        Uses the current class attributes :py:attr:`x0`, :py:attr:`z0` and :py:attr:`u0` to create the initial guess.
        The initial guess is simply the initial values for all :math:`k=0,\dots,N` instances of :math:`x_k`, :math:`u_k` and :math:`z_k`.

        .. warning::
            If no initial values for :py:attr:`x0`, :py:attr:`z0` and :py:attr:`u0` were supplied during setup, these default to zero.

        .. note::
            The initial guess is fully customizable by directly setting values on the class attribute:
            :py:attr:`opt_x_num`.
        """
        assert self.flags['setup'] == True, 'MPC was not setup yet. Please call MPC.setup().'

        self.opt_x_num['_x'] = self._x0.cat/self._x_scaling
        self.opt_x_num['_u'] = self._u0.cat/self._u_scaling
        self.opt_x_num['_z'] = self._z0.cat/self._z_scaling

        self.flags['set_initial_guess'] = True


    def make_step(self, x0):
        """Main method of the class during runtime. This method is called at each timestep
        and returns the control input for the current initial state :py:obj:`x0`.

        The method prepares the MHE by setting the current parameters, calls :py:func:`solve`
        and updates the :py:class:`do_mpc.data.Data` object.

        :param x0: Current state of the system.
        :type x0: numpy.ndarray or casadi.DM

        :return: u0
        :rtype: numpy.ndarray
        """
        # Check setup.
        assert self.flags['setup'] == True, 'MPC was not setup yet. Please call MPC.setup().'

        # Check input type.
        if isinstance(x0, (np.ndarray, casadi.DM)):
            pass
        elif isinstance(x0, structure3.DMStruct):
            x0 = x0.cat
        else:
            raise Exception('Invalid type {} for x0. Must be {}'.format(type(x0), (np.ndarray, casadi.DM, structure3.DMStruct)))

        # Check input shape.
        n_val = np.prod(x0.shape)
        assert n_val == self.model.n_x, 'Wrong input with shape {}. Expected vector with {} elements'.format(n_val, self.model.n_x)
        # Check (once) if the initial guess was supplied.
        if not self.flags['set_initial_guess']:
            warnings.warn('Intial guess for the MPC was not set. The solver call is likely to fail.')
            time.sleep(5)
            # Since do-mpc is warmstarting, the initial guess will exist after the first call.
            self.flags['set_initial_guess'] = True

        # Get current tvp, p and time (as well as previous u)
        u_prev = self._u0
        tvp0 = self.tvp_fun(self._t0)
        p0 = self.p_fun(self._t0)
        t0 = self._t0

        # Set the current parameter struct for the optimization problem:
        self.opt_p_num['_x0'] = x0
        self.opt_p_num['_u_prev'] = u_prev
        self.opt_p_num['_tvp'] = tvp0['_tvp']
        self.opt_p_num['_p'] = p0['_p']
        # Solve the optimization problem (method inherited from optimizer)
        self.solve()

        # Extract solution:
        u0 = self.opt_x_num['_u', 0, 0]*self._u_scaling
        z0 = self.opt_x_num['_z', 0, 0, 0]*self._z_scaling
        aux0 = self.opt_aux_num['_aux', 0, 0]

        # Store solution:
        self.data.update(_x = x0)
        self.data.update(_u = u0)
        self.data.update(_z = z0)
        self.data.update(_tvp = tvp0['_tvp', 0])
        self.data.update(_time = t0)
        self.data.update(_aux = aux0)

        # Store additional information
        self.data.update(opt_p_num = self.opt_p_num)
        if self.store_full_solution == True:
            opt_x_num_unscaled = self.opt_x_num_unscaled
            opt_aux_num = self.opt_aux_num
            self.data.update(_opt_x_num = opt_x_num_unscaled)
            self.data.update(_opt_aux_num = opt_aux_num)
        if self.store_lagr_multiplier == True:
            lam_g_num = self.lam_g_num
            self.data.update(_lam_g_num = lam_g_num)
        if len(self.store_solver_stats) > 0:
            solver_stats = self.solver_stats
            store_solver_stats = self.store_solver_stats
            self.data.update(**{stat_i: value for stat_i, value in solver_stats.items() if stat_i in store_solver_stats})

        # Update initial
        self._t0 = self._t0 + self.t_step
        self._x0.master = x0
        self._u0.master = u0
        self._z0.master = z0

        # Return control input:
        return u0.full()


    def _update_bounds(self):
        """Private method to update the bounds of the optimization variables based on the current values defined with :py:attr:`scaling`.
        """
        if self.cons_check_colloc_points:   # Constraints for all collocation points.
            # Dont bound the initial state
            self.lb_opt_x['_x', 1:self.n_horizon] = self._x_lb.cat
            self.ub_opt_x['_x', 1:self.n_horizon] = self._x_ub.cat

            # Bounds for the algebraic variables:
            self.lb_opt_x['_z'] = self._z_lb.cat
            self.ub_opt_x['_z'] = self._z_ub.cat

            # Terminal bounds
            self.lb_opt_x['_x', self.n_horizon, :, -1] = self._x_terminal_lb.cat
            self.ub_opt_x['_x', self.n_horizon, :, -1] = self._x_terminal_ub.cat
        else:   # Constraints only at the beginning of the finite Element
            # Dont bound the initial state
            self.lb_opt_x['_x', 1:self.n_horizon, :, -1] = self._x_lb.cat
            self.ub_opt_x['_x', 1:self.n_horizon, :, -1] = self._x_ub.cat

            # Bounds for the algebraic variables:
            self.lb_opt_x['_z', :, :, 0] = self._z_lb.cat
            self.ub_opt_x['_z', :, : ,0] = self._z_ub.cat

            # Terminal bounds
            self.lb_opt_x['_x', self.n_horizon, :, -1] = self._x_terminal_lb.cat
            self.ub_opt_x['_x', self.n_horizon, :, -1] = self._x_terminal_ub.cat

        # Bounds for the inputs along the horizon
        self.lb_opt_x['_u'] = self._u_lb.cat
        self.ub_opt_x['_u'] = self._u_ub.cat

        # Bounds for the slack variables:
        self.lb_opt_x['_eps'] = self._eps_lb.cat
        self.ub_opt_x['_eps'] = self._eps_ub.cat


    def _prepare_nlp(self):
        """Internal method. See detailed documentation with optimizer.prepare_nlp
        """
        nl_cons_input = self.model['x', 'u', 'z', 'tvp', 'p']
        self._setup_nl_cons(nl_cons_input)
        self._check_validity()

        # Obtain an integrator (collocation, discrete-time) and the amount of intermediate (collocation) points
        ifcn, n_total_coll_points = self._setup_discretization()
        n_branches, n_scenarios, child_scenario, parent_scenario, branch_offset = self._setup_scenario_tree()

        # How many scenarios arise from the scenario tree (robust multi-stage MPC)
        n_max_scenarios = self.n_combinations ** self.n_robust

        # If open_loop option is active, all scenarios (at a given stage) have the same input.
        if self.open_loop:
            n_u_scenarios = 1
        else:
            # Else: Each scenario has its own input.
            n_u_scenarios = n_max_scenarios

        # How many slack variables (for soft constraints) are introduced over the horizon.
        if self.nl_cons_single_slack:
            n_eps = 1
        else:
            n_eps = self.n_horizon

        # Create struct for optimization variables:
        self._opt_x = opt_x = self.model.sv.sym_struct([
            # One additional point (in the collocation dimension) for the final point.
            entry('_x', repeat=[self.n_horizon+1, n_max_scenarios,
                                1+n_total_coll_points], struct=self.model._x),
            entry('_z', repeat=[self.n_horizon, n_max_scenarios,
                                max(n_total_coll_points,1)], struct=self.model._z),
            entry('_u', repeat=[self.n_horizon, n_u_scenarios], struct=self.model._u),
            entry('_eps', repeat=[n_eps, n_max_scenarios], struct=self._eps),
        ])
        self.n_opt_x = self._opt_x.shape[0]
        # NOTE: The entry _x[k,child_scenario[k,s,b],:] starts with the collocation points from s to b at time k
        #       and the last point contains the child node
        # NOTE: Currently there exist dummy collocation points for the initial state (for each branch)

        # Create scaling struct as assign values for _x, _u, _z.
        self.opt_x_scaling = opt_x_scaling = opt_x(1)
        opt_x_scaling['_x'] = self._x_scaling
        opt_x_scaling['_z'] = self._z_scaling
        opt_x_scaling['_u'] = self._u_scaling
        # opt_x are unphysical (scaled) variables. opt_x_unscaled are physical (unscaled) variables.
        self.opt_x_unscaled = opt_x_unscaled = opt_x(opt_x.cat * opt_x_scaling)


        # Create struct for optimization parameters:
        self._opt_p = opt_p = self.model.sv.sym_struct([
            entry('_x0', struct=self.model._x),
            entry('_tvp', repeat=self.n_horizon+1, struct=self.model._tvp),
            entry('_p', repeat=self.n_combinations, struct=self.model._p),
            entry('_u_prev', struct=self.model._u),
        ])
        _w = self.model._w(0)

        self.n_opt_p = opt_p.shape[0]

        # Dummy struct with symbolic variables
        self.aux_struct = self.model.sv.sym_struct([
            entry('_aux', repeat=[self.n_horizon, n_max_scenarios], struct=self.model._aux_expression)
        ])
        # Create mutable symbolic expression from the struct defined above.
        self._opt_aux = opt_aux = self.model.sv.struct(self.aux_struct)

        self.n_opt_aux = opt_aux.shape[0]

        self._lb_opt_x = opt_x(-np.inf)
        self._ub_opt_x = opt_x(np.inf)

        # Initialize objective function and constraints
        obj = DM(0)
        cons = []
        cons_lb = []
        cons_ub = []

        # Initial condition:
        cons.append(opt_x['_x', 0, 0, -1]-opt_p['_x0']/self._x_scaling)

        cons_lb.append(np.zeros((self.model.n_x, 1)))
        cons_ub.append(np.zeros((self.model.n_x, 1)))

        # NOTE: Weigthing factors for the tree assumed equal. They could be set from outside
        # Weighting factor for every scenario
        omega = [1. / n_scenarios[k + 1] for k in range(self.n_horizon)]
        omega_delta_u = [1. / n_scenarios[k + 1] for k in range(self.n_horizon)]

        # For all control intervals
        for k in range(self.n_horizon):
            # For all scenarios (grows exponentially with n_robust)
            for s in range(n_scenarios[k]):
                # For all childen nodes of each node at stage k, discretize the model equations

                # Scenario index for u is always 0 if self.open_loop = True
                s_u = 0 if self.open_loop else s
                for b in range(n_branches[k]):
                    # Obtain the index of the parameter values that should be used for this scenario
                    current_scenario = b + branch_offset[k][s]

                    # Compute constraints and predicted next state of the discretization scheme
                    col_xk = vertcat(*opt_x['_x', k+1, child_scenario[k][s][b], :-1])
                    col_zk = vertcat(*opt_x['_z', k, child_scenario[k][s][b]])
                    [g_ksb, xf_ksb] = ifcn(opt_x['_x', k, s, -1], col_xk,
                                           opt_x['_u', k, s_u], col_zk, opt_p['_tvp', k],
                                           opt_p['_p', current_scenario], _w)

                    # Add the collocation equations
                    cons.append(g_ksb)
                    cons_lb.append(np.zeros(g_ksb.shape[0]))
                    cons_ub.append(np.zeros(g_ksb.shape[0]))

                    # Add continuity constraints
                    cons.append(xf_ksb - opt_x['_x', k+1, child_scenario[k][s][b], -1])
                    cons_lb.append(np.zeros((self.model.n_x, 1)))
                    cons_ub.append(np.zeros((self.model.n_x, 1)))

                    k_eps = min(k, n_eps-1)
                    if self.nl_cons_check_colloc_points:
                        # Ensure nonlinear constraints on all collocation points
                        for i in range(n_total_coll_points):
                            nl_cons_k = self._nl_cons_fun(
                                opt_x_unscaled['_x', k, s, i], opt_x_unscaled['_u', k, s_u], opt_x_unscaled['_z', k, s, i],
                                opt_p['_tvp', k], opt_p['_p', current_scenario], opt_x_unscaled['_eps', k_eps, s])
                            cons.append(nl_cons_k)
                            cons_lb.append(self._nl_cons_lb)
                            cons_ub.append(self._nl_cons_ub)
                    else:
                        # Ensure nonlinear constraints only on the beginning of the FE
                        nl_cons_k = self._nl_cons_fun(
                            opt_x_unscaled['_x', k, s, -1], opt_x_unscaled['_u', k, s_u], opt_x_unscaled['_z', k, s, 0],
                            opt_p['_tvp', k], opt_p['_p', current_scenario], opt_x_unscaled['_eps', k_eps, s])
                        cons.append(nl_cons_k)
                        cons_lb.append(self._nl_cons_lb)
                        cons_ub.append(self._nl_cons_ub)

                    # Add terminal constraints
                    # TODO: Add terminal constraints with an additional nl_cons

                    # Add contribution to the cost
                    obj += omega[k] * self.lterm_fun(opt_x_unscaled['_x', k, s, -1], opt_x_unscaled['_u', k, s_u],
                                                     opt_x_unscaled['_z', k, s, -1], opt_p['_tvp', k], opt_p['_p', current_scenario])
                    # Add slack variables to the cost
                    obj += self.epsterm_fun(opt_x_unscaled['_eps', k_eps, s])

                    # In the last step add the terminal cost too
                    if k == self.n_horizon - 1:
                        obj += omega[k] * self.mterm_fun(opt_x_unscaled['_x', k + 1, s, -1], opt_p['_tvp', k+1],
                                                         opt_p['_p', current_scenario])

                    # U regularization:
                    if k == 0:
                        obj += self.rterm_factor.cat.T@((opt_x['_u', 0, s_u]-opt_p['_u_prev']/self._u_scaling)**2)
                    else:
                        obj += self.rterm_factor.cat.T@((opt_x['_u', k, s_u]-opt_x['_u', k-1, parent_scenario[k][s_u]])**2)

                    # Calculate the auxiliary expressions for the current scenario:
                    opt_aux['_aux', k, s] = self.model._aux_expression_fun(
                        opt_x_unscaled['_x', k, s, -1], opt_x_unscaled['_u', k, s_u], opt_x_unscaled['_z', k, s, -1], opt_p['_tvp', k], opt_p['_p', current_scenario])

                    # For some reason when working with MX, the "unused" aux values in the scenario tree must be set explicitly (they are not ever used...)
                for s_ in range(n_scenarios[k],n_max_scenarios):
                    opt_aux['_aux', k, s_] = self.model._aux_expression_fun(
                        opt_x_unscaled['_x', k, s, -1], opt_x_unscaled['_u', k, s_u], opt_x_unscaled['_z', k, s, -1], opt_p['_tvp', k], opt_p['_p', current_scenario])

        # Set bounds for all optimization variables
        self._update_bounds()


        # Write all created elements to self:
        self._nlp_obj = obj
        self._nlp_cons = cons
        self._nlp_cons_lb = cons_lb
        self._nlp_cons_ub = cons_ub


        # Initialize copies of structures with numerical values (all zero):
        self._opt_x_num = self._opt_x(0)
        self.opt_x_num_unscaled = self._opt_x(0)
        self._opt_p_num = self._opt_p(0)
        self.opt_aux_num = self._opt_aux(0)

        self.flags['prepare_nlp'] = True

    def _create_nlp(self):
        """Internal method. See detailed documentation in optimizer.create_nlp
        """


        self._nlp_cons = vertcat(*self._nlp_cons)
        self._nlp_cons_lb = vertcat(*self._nlp_cons_lb)
        self._nlp_cons_ub = vertcat(*self._nlp_cons_ub)

        self.n_opt_lagr = self._nlp_cons.shape[0]
        # Create casadi optimization object:
        nlpsol_opts = {
            'expand': False,
            'ipopt.linear_solver': 'mumps',
        }.update(self.nlpsol_opts)
        nlp = {'x': vertcat(self._opt_x), 'f': self._nlp_obj, 'g': self._nlp_cons, 'p': vertcat(self._opt_p)}
        self.S = nlpsol('S', 'ipopt', nlp, self.nlpsol_opts)


        # Create function to caculate all auxiliary expressions:
        self.opt_aux_expression_fun = Function('opt_aux_expression_fun', [self._opt_x, self._opt_p], [self._opt_aux])


        # Gather meta information:
        meta_data = {key: getattr(self, key) for key in self.data_fields}
        meta_data.update({'structure_scenario': self.scenario_tree['structure_scenario']})
        self.data.set_meta(**meta_data)

        self._prepare_data()

        self.flags['setup'] = True
