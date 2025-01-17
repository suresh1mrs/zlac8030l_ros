#!/usr/bin/env python
"""
BSD 3-Clause License
Copyright (c) 2022, Mohamed Abdelkader Zahana
All rights reserved.
Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.
THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

"""
ROS driver for the ZLAC8030L motor controller
Reuquires: ZALAC8030L_CAN_controller, https://github.com/mzahana/ZLAC8030L_CAN_controller
"""

from time import time
import rospy
import tf
from geometry_msgs.msg import Twist
from std_msgs.msg import Float64
from nav_msgs.msg import Odometry
from ZLAC8030L_CAN_controller.canopen_controller import MotorController
from differential_drive import DiffDrive
from pid import PID
from zlac8030l_ros.msg import State

class Driver:
    def __init__(self):
        self._can_channel = rospy.get_param("~can_channel", "can0")
        self._bus_type = rospy.get_param("~bus_type", "socketcan")
        self._bitrate = rospy.get_param("~bitrate", 500000)
        self._eds_file = rospy.get_param("~eds_file","")
        # self._wheel_ids = rospy.get_param("~wheel_ids", []) # TODO needs checking
        self._wheel_ids = {"fl":1, "bl":2, "br":3, "fr":4}
        self._flip_direction = {"fl": -1, "bl": -1, "br": 1, "fr": 1}

        # Velocity vs. Troque modes
        self._torque_mode = rospy.get_param("~torque_mode", False)

        self._kp = rospy.get_param("~vel_kp", 200)
        self._ki = rospy.get_param("~vel_ki", 10)
        self._kd = rospy.get_param("~vel_kd", 0)
        # Create PIDs, one for each wheel
        self._vel_pids = {"fl":PID(kp=self._kp, ki=self._ki, kd=self._kd), "bl":PID(kp=self._kp, ki=self._ki, kd=self._kd), "br":PID(kp=self._kp, ki=self._ki, kd=self._kd), "fr":PID(kp=self._kp, ki=self._ki, kd=self._kd)}

        
        # Stores current wheel speeds [rpm]
        self._current_whl_rpm = {"fl": 0.0, "bl": 0.0, "br": 0.0, "fr": 0.0}
        # Target RPM
        self._target_whl_rpm = {"fl": 0.0, "bl": 0.0, "br": 0.0, "fr": 0.0}
        # Target torque; when slef._control_mode="torque"
        self._target_current = {"fl": 0.0, "bl": 0.0, "br": 0.0, "fr": 0.0}
        
        self._wheel_radius = rospy.get_param("~wheel_radius", 0.194)

        self._track_width = rospy.get_param("~track_width", 0.8)

        self._max_vx = rospy.get_param("~max_vx", 2.0)
        self._max_w = rospy.get_param("~max_w", 1.57)

        # Max linear accelration [m/s^2] >0
        self._max_lin_accel = rospy.get_param("~max_lin_accel", 10)
        # Max angular accelration [rad/s^2] >0
        self._max_ang_accel = rospy.get_param("~max_ang_accel", 15)

        self._odom_frame = rospy.get_param("~odom_frame", "odom_link")
        self._robot_frame = rospy.get_param("~robot_frame", "base_link")

        self._loop_rate = rospy.get_param("~loop_rate", 100.0)

        self._cmd_timeout = rospy.get_param("~cmd_timeout", 0.1)

        self._diff_drive = DiffDrive(self._wheel_radius, self._track_width)

        # last time stamp. Used in odometry calculations
        self._last_odom_dt = time()

        # Last time a velcoity command was received
        self._last_cmd_t = time()

        # If True, odom TF will be published
        self._pub_tf = rospy.get_param("~pub_tf", False)


        try:
            if (self._torque_mode):
                mode='torque'
            else:
                 mode='velocity'   
            self._network = MotorController(channel=self._can_channel, bustype=self._bus_type, bitrate=self._bitrate, node_ids=None, debug=True, eds_file=self._eds_file, mode=mode)
        except Exception as e:
            rospy.logerr("Could not create CAN network object. Error: %s", e)
            exit(0)

        rospy.logwarn("\n ** cmd_vel must be published at rate more than %s Hz ** \n", 1/self._cmd_timeout)

        # ------------------- Subscribers ----------------#
        rospy.Subscriber("cmd_vel", Twist, self.cmdVelCallback)

        # ------------------- Publishers ----------------#
        self._odom_pub = rospy.Publisher("odom", Odometry, queue_size=10)
        self._vel_pub = rospy.Publisher("forward_vel", Float64, queue_size=10)

        self._motor_state_pub_dict = {}
        for wheel in ["fl", "bl", "br", "fr"]:
            self._motor_state_pub_dict[wheel] = rospy.Publisher(wheel+"_motor/state", State, queue_size=10)

        # ------------------- Services ----------------#

        # TF broadcaster
        self._tf_br = tf.TransformBroadcaster()

        rospy.loginfo("** Driver initialization is done **\n")

    def rpmToRps(self, rpm):
        return rpm / 9.5493

    def rpsToRpm(self, rad):
        return rad * 9.5493

    def applyControls(self):
        """
        Computes and applyies control signals based on the control mode (velocity vs. torque)
        """
        if (self._torque_mode):
            try:
                err_rpm = {"fr":0, "fl":0, "br":0, "bl":0}
                for t in ["fl", "bl", "br", "fr"]:
                    v_dict = self._network.getVelocity(node_id=self._wheel_ids[t])
                    vel = v_dict['value']* self._flip_direction[t] # flipping is required for odom
                    self._current_whl_rpm[t] = v_dict['value']

                    err_rpm[t] = self._target_whl_rpm[t] - self._current_whl_rpm[t]
                    self._target_current[t] = self._vel_pids["fr"].update(err_rpm[t])

            except Exception as e:
                rospy.logerr_throttle(1, "[applyControls] Error in getting wheel velocity: %s. Check driver connection", e)

        
        
            try:
                for t in ["fl", "bl", "br", "fr"]:
                    self._network.setTorque( node_id=self._wheel_ids[t], current_mA=self._target_current[t])
            except Exception as e:
                rospy.logerr_throttle(1, "[applyControls] Error in setting wheel torque: %s", e)
        else:
            # Send target velocity to the controller
            try:
                for t in ["fl", "bl", "br", "fr"]:
                    self._network.setVelocity(node_id=self._wheel_ids[t], vel=self._target_whl_rpm[t])
                
            except Exception as e:
                rospy.logerr_throttle(1, "[applyControls] Error in setting wheel velocity: %s", e)


    
    def cmdVelCallback(self, msg):
        sign_x = -1 if msg.linear.x <0 else 1
        sign_w = -1 if msg.angular.z <0 else 1
        
        vx = msg.linear.x
        w = msg.angular.z

        # Initialize final commanded velocities,, after applying constraitns
        v_d = vx
        w_d = w

        # Limit velocity by acceleration
        current_t = time()
        dt = current_t - self._last_cmd_t
        self._last_cmd_t = current_t
        odom = self._diff_drive.calcRobotOdom(dt)
        current_v = odom['v']
        current_w = odom['w']

        # Figure out the max acceleration sign
        dv = vx-current_v
        abs_dv = abs(dv)
        if (abs_dv > 0):
            lin_acc = (abs_dv/dv)*self._max_lin_accel
        else:
            lin_acc = self._max_lin_accel

        dw = w-current_w
        abs_dw = abs(w-current_w)
        if (abs_dw > 0):
            ang_acc = dw/abs_dw * self._max_ang_accel
        else:
            ang_acc = self._max_ang_accel

        # Maximum acceptable velocity given the acceleration constraints, and current velocity
        max_v = current_v + dt*lin_acc
        max_w = current_w + dt*ang_acc

        # Compute & compare errors to deceide whether to scale down the desired velocity
        # For linear vel
        ev_d = abs(vx-current_v)
        ev_max = abs(max_v - current_v)
        if ev_d > ev_max:
            v_d=max_v

        # For angular vel
        ew_d = abs(w-current_w)
        ew_max = abs(max_w - current_w)
        if ew_d > ew_max:
            w_d = max_w

        if (abs(v_d) > self._max_vx):
            rospy.logwarn_throttle(1, "Commanded linear velocity %s is more than maximum magnitude %s", sign_x*vx, sign_x*self._max_vx)
            v_d = sign_x * self._max_vx
        if (abs(w_d) > self._max_w):
            rospy.logwarn_throttle(1, "Commanded angular velocity %s is more than maximum magnitude %s", sign_w*w, sign_w*self._max_w)
            w_d = sign_w * self._max_w

        # Compute wheels velocity commands [rad/s]
        (wl, wr) = self._diff_drive.calcWheelVel(v_d,w_d)

        # TODO convert rad/s to rpm
        wl_rpm = self.rpsToRpm(wl)
        wr_rpm = self.rpsToRpm(wr)
        self._target_whl_rpm["fl"] = wl_rpm * self._flip_direction["fl"]
        self._target_whl_rpm["bl"] = wl_rpm * self._flip_direction["bl"]
        self._target_whl_rpm["br"] = wr_rpm * self._flip_direction["br"]
        self._target_whl_rpm["fr"] = wr_rpm * self._flip_direction["fr"]

        # Apply control in the main loop
        

        

    def pubOdom(self):
        """Computes & publishes odometry msg
        """
        try:
            for t in ["fl", "bl", "br", "fr"]:

                v_dict = self._network.getVelocity(node_id=self._wheel_ids[t])
                vel = v_dict['value']* self._flip_direction[t] # flipping is required for odom
                self._current_whl_rpm[t] = v_dict['value']
                if t=="fl":
                    self._diff_drive._fl_vel = self.rpmToRps(vel)
                if t=="fr":
                    self._diff_drive._fr_vel = self.rpmToRps(vel)
                if t=="bl":
                    self._diff_drive._bl_vel = self.rpmToRps(vel)
                if t=="br":
                    self._diff_drive._br_vel = self.rpmToRps(vel)
        except Exception as e :
            rospy.logerr_throttle(1, " Error in pubOdom: %s. Check driver connection", e)
            #rospy.logerr_throttle(1, "Availabled nodes = %s", self._network._network.scanner.nodes)

        now = time()

        dt= now - self._last_odom_dt
        self._last_odom_dt = now

        odom = self._diff_drive.calcRobotOdom(dt)

        msg = Odometry()

        time_stamp = rospy.Time.now()
        msg.header.stamp = time_stamp
        msg.header.frame_id=self._odom_frame
        msg.child_frame_id = self._robot_frame

        msg.pose.pose.position.x = odom["x"]
        msg.pose.pose.position.y = odom["y"]
        msg.pose.pose.position.z = 0.0
        odom_quat = tf.transformations.quaternion_from_euler(0, 0, odom['yaw'])
        msg.pose.pose.orientation.x = odom_quat[0]
        msg.pose.pose.orientation.y = odom_quat[1]
        msg.pose.pose.orientation.z = odom_quat[2]
        msg.pose.pose.orientation.w = odom_quat[3]
        # pose covariance
        msg.pose.covariance[0] = 1000.0 # x-x
        msg.pose.covariance[7] = 1000.0 # y-y
        msg.pose.covariance[14] = 1000.0 # z-z
        msg.pose.covariance[21] = 1000.0 # roll
        msg.pose.covariance[28] = 1000.0 # pitch
        msg.pose.covariance[35] = 1000.0 # yaw

        # For twist, velocities are w.r.t base_link. So, only x component (forward vel) is used
        msg.twist.twist.linear.x = odom['v']
        msg.twist.twist.linear.y = 0 #odom['y_dot']
        msg.twist.twist.angular.z = odom['w']
        msg.twist.covariance[0] = 0.1 # vx
        msg.twist.covariance[7] = 0.1 # vx
        msg.twist.covariance[14] = 1000.0 # vz
        msg.twist.covariance[21] = 1000.0 # omega_x
        msg.twist.covariance[28] = 1000.0 # omega_y
        msg.twist.covariance[35] = 0.1 # omega_z
        
        self._odom_pub.publish(msg)
        if self._pub_tf:
            # Send TF
            self._tf_br.sendTransform((odom['x'],odom['y'],0),odom_quat,time_stamp,self._robot_frame,self._odom_frame)

        msg = Float64()
        msg.data = odom["v"] # Forward velocity
        self._vel_pub.publish(msg)

    def pubMotorState(self):
        for t in ["fl", "bl", "br", "fr"]:
            msg = State()
            msg.header.stamp = rospy.Time.now()
            msg.node_id = self._wheel_ids[t]
            
            # Voltage
            try:
                volts_dict = self._network.getVoltage(self._wheel_ids[t])
                volts = volts_dict['value']
                msg.voltage = volts
            except:
                pass

            # Target current in mA
            msg.target_current_mA = self._target_current[t]
            # Target current in A
            msg.target_current_A = self._target_current[t]/1000.0
            
            # Motor current
            try:
                curr_dict = self._network.getMotorCurrent(self._wheel_ids[t])
                curr = curr_dict['value']
                msg.current = curr
            except:
                pass

            # Error Code
            try:
                err_dict = self._network.getErrorCode(self._wheel_ids[t])
                code = err_dict['value']
                msg.error_code = code
            except:
                pass

            # Current speed, rpm
            try:
                msg.actual_speed = self._current_whl_rpm[t]
            except:
                pass

            # Target speed, rpm
            try:
                msg.target_speed = self._target_whl_rpm[t]
            except:
                pass

            self._motor_state_pub_dict[t].publish(msg)


    def mainLoop(self):
        rate = rospy.Rate(self._loop_rate)

        while not rospy.is_shutdown():
            now = time()

            dt = now - self._last_cmd_t
            if (dt > self._cmd_timeout):
                # set zero velocity
                for t in ["fl", "bl", "br", "fr"]:
                    self._target_whl_rpm[t]=0.0

            # Apply controls
            self.applyControls()

            # Publish wheel odom
            self.pubOdom()
            # Publish Motors state
            self.pubMotorState()
            
            rate.sleep()
  

if __name__ == "__main__":
    rospy.init_node("** Motor driver node started ** \n",anonymous=True)
    try:
        driver = Driver()

        driver.mainLoop()
    except rospy.ROSInterruptException:
        driver._network.disconnectNetwork()