#!/usr/bin/env python

# Copyright (c) 2013, University Of Massachusetts Lowell
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
# 3. Neither the name of the University of Massachusetts Lowell nor the names
#    from of its contributors may be used to endorse or promote products
#    derived this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""

  Baxter Teleoperation using Razer Hydra

"""

import sys
import threading

import roslib
roslib.load_manifest('baxter_hydra_teleop')
import rospy
import tf

from razer_hydra.msg import Hydra

from geometry_msgs.msg import (
    PoseStamped,
    Pose,
    Point,
    Quaternion,
    Transform,
)
from std_msgs.msg import Header
from baxter_msgs.srv import SolvePositionIK
from baxter_msgs.srv import SolvePositionIKRequest
import baxter_interface

import baxter_faces

import vis


class HeadMover(object):
    def __init__(self):
        self._head = baxter_interface.Head()
        self.pan_angle = 0

    def set_pose(self):
        self._head.set_pan(self.pan_angle)

    def parse_joy(self, joypad):
        if joypad.joy[0] != 0:
            increment = -joypad.joy[0] / 50
            self.pan_angle += increment
            if abs(self.pan_angle) > 1.57:
                self.pan_angle -= increment
            self.set_pose()


class LimbMover(object):
    def __init__(self, limb):
        self.limb = limb
        self.interface = baxter_interface.Limb(limb)
        self.solver = IKSolver(limb)
        self.last_solve_request_time = rospy.Time.now()
        self.running = True
        self.thread = threading.Thread(target=self._update_thread)
        self.vis = vis.Vis()
        self.goal_transform = GoalTransform(limb)

    def enable(self):
        self.thread.start()

    def set_target(self, joints):
        self.target_joints = joints

    def _update_req_time(self):
        self.last_solve_request_time = rospy.Time.now()

    def _solver_cooled_down(self):
        time_since_req = rospy.Time.now() - self.last_solve_request_time
        return time_since_req > rospy.Duration(0.05)  # 20 Hz

    def update(self, trigger, gripper_travel):
        self.vis.show_gripper(self.limb, gripper_travel, 0.026, 0.11, 1)
        self.goal_transform.update()

        # Throttle service requests
        if trigger and self._solver_cooled_down():
            self._update_req_time()
            return self.solver.solve()
        return True

    def stop_thread(self):
        if self.thread.is_alive():
            self.running = False
            self.thread.join()

    def _update_thread(self):
        rospy.loginfo("Starting Joint Update Thread: %s\n" % self.limb)
        rate = rospy.Rate(200)
        while not rospy.is_shutdown() and self.running:
            self.interface.set_joint_positions(self.solver.solution)
            rate.sleep()
        rospy.loginfo("Stopped %s" % self.limb)


class IKSolver(object):
    def __init__(self, limb):
        self.limb = limb
        ns = "/sdk/robot/limb/" + self.limb + "/solve_ik_position"
        rospy.wait_for_service(ns)
        self.iksvc = rospy.ServiceProxy(ns, SolvePositionIK)
        self.solution = dict()
        self.mapping = [
            # Human-like mapping (front of camera  = front of the wrist)
            Quaternion(
                x=0,
                y=0.7071067811865475244,  # sqrt(0.5)
                z=0,
                w=0.7071067811865475244  # sqrt(0.5)
            # Camera is pointing down
            ), Quaternion(
                x=0,
                y=1,
                z=0,
                w=0
            ),
        ]

    def solve(self):
        ikreq = SolvePositionIKRequest()
        hdr = Header(
            stamp=rospy.Time.now(), frame_id=self.limb + '_gripper_goal')
        pose = PoseStamped(
            header=hdr,
            pose=Pose(
                position=Point(x=0, y=0, z=0),
                orientation=self.mapping[1]
            ),
        )

        ikreq.pose_stamp.append(pose)
        try:
            resp = self.iksvc(ikreq)
        except rospy.ServiceException, e:
            rospy.loginfo("Service call failed: %s" % (e,))

        if (resp.isValid[0]):
            self.solution = dict(
                zip(resp.joints[0].names, resp.joints[0].angles))
            rospy.loginfo("Solution Found, %s" % self.limb, self.solution)
            return True

        else:
            rospy.logwarn("INVALID POSE for %s" % self.limb)
            return False


class GoalTransform(object):
    """ Publish an additional transforms constrained in some way.

    Should allow to make it easier to add teleoperation constraints,
    e.g. lock the control plane, or change motion scaling.
    """

    def __init__(self, limb):
        self.modes = {
                      "identity": self._identity,
                      "orientation": self._orientation_lock,
                      "plane": self._plane_lock
                      }
        self.set_mode("identity")
        self.br = tf.TransformBroadcaster()
        self.plane = Transform()
        self.plane.rotation.w = 1
        self.limb = limb
        self.orientation = Quaternion(0, 0, 0, 1)
        self.orientation_lock_frame = "base"
        self.tf_listener = tf.TransformListener()
        """
        _tf_offset is used to add an offset to the stamp of published
        transforms. It is a hack needed to IK nodes on Baxter happy, and
        would be different for different systems.
        """
        self._tf_offset = 0.1

        self.plane = Pose()

    def set_mode(self, mode):
        self.updater = self.modes[mode]

    def update(self):
        self.updater()

    def _identity(self):
        """ No tranformation, 1 to 1 mapping """
        self.br.sendTransform(
           (0, 0, 0),
           (0, 0, 0, 1),
           rospy.Time.now() + rospy.Duration(self._tf_offset),
            self.limb + "_gripper_goal",
           "hydra_" + self.limb + "_grab")

    def _orientation_lock(self):
        """ Lock the orientation """
        try:
            (trans, rot) = self.tf_listener.lookupTransform(
                   self.orientation_lock_frame,
                   'hydra_' + self.limb + '_grab',
                   rospy.Time(0))
        except (tf.LookupException,
                tf.ConnectivityException,
                tf.ExtrapolationException) as e:
            print e
            return

        self.br.sendTransform(
           trans,
           (
            self.orientation.x,
            self.orientation.y,
            self.orientation.z,
            self.orientation.w,
           ),
           rospy.Time.now() + rospy.Duration(self._tf_offset),
           self.limb + "_gripper_goal",
           self.orientation_lock_frame)

    def _plane_lock(self):
        """ Lock the orientation """
        pass


class Teleop(object):

    def __init__(self):
        rospy.init_node("baxter_hydra_teleop")
        self.status_display = baxter_faces.FaceImage()
        rospy.loginfo("Getting robot state... ")
        self.rs = baxter_interface.RobotEnable()
        self.hydra_msg = Hydra()
        self.hydra_msg_lock = threading.Lock()

        self.gripper_left = baxter_interface.Gripper("left")
        self.gripper_right = baxter_interface.Gripper("right")
        self.mover_left = LimbMover("left")
        self.mover_right = LimbMover("right")
        self.mover_head = HeadMover()
        self.happy_count = 0  # Need inertia on how long unhappy is displayed
        self.hydra_msg = Hydra()

        rospy.on_shutdown(self._cleanup)
        sub = rospy.Subscriber("/hydra_calib", Hydra, self._hydra_cb)

        rospy.Timer(rospy.Duration(1.0 / 30), self._main_loop)

        rospy.loginfo(
          "Press left or right button on Hydra to start the teleop")
        while not self.rs.state().enabled and not rospy.is_shutdown():
            rospy.Rate(10).sleep()
        self.mover_left.enable()
        self.mover_right.enable()
        self.mover_head.set_pose()

    def _reset_gripper(self, gripper):
        gripper.reboot()
        gripper.set_force(10)
        gripper.set_holding_force(20)
        gripper.set_dead_band(5)
        if not gripper.ready():
            gripper.calibrate()
        gripper.set_position(0)

    def _reset_grippers(self, event):
        rospy.loginfo('Resetting grippers')
        self._reset_gripper(self.gripper_right)
        self._reset_gripper(self.gripper_left)

    def _enable(self):
        rospy.loginfo("Enabling robot... ")
        self.rs.enable()
        rospy.Timer(rospy.Duration(0.1), self._reset_grippers, oneshot=True)
        self.status_display.set_image('happy')

    def _hydra_cb(self, msg):
        with self.hydra_msg_lock:
            self.hydra_msg = msg

    def _main_loop(self, event):
        if self.rs.state().estop_button == 1:
            self.status_display.set_image('dead')
            return
        else:
            if not self.rs.state().enabled:
                self.status_display.set_image('indifferent')

        with self.hydra_msg_lock:
            msg = self.hydra_msg

        self._terminate_if_pressed(msg)

        self.mover_left.update(False, 1 - self.gripper_left.position() / 100)
        self.mover_right.update(False, 1 - self.gripper_right.position() / 100)

        if not self.rs.state().enabled:
            if msg.paddles[0].buttons[0] or msg.paddles[1].buttons[0]:
                self._enable()
            return

        if not rospy.is_shutdown():
            happy0 = self.mover_left.update(
                msg.paddles[0].buttons[0],
                1 - self.gripper_left.position() / 100)
            happy1 = self.mover_right.update(
                msg.paddles[1].buttons[0],
                1 - self.gripper_right.position() / 100)
            if happy0 and happy1:
                self.happy_count += 1
                if self.happy_count > 200:
                    self.status_display.set_image('happy')
            else:
                self.happy_count = 0
                self.status_display.set_image('confused')

            self.mover_head.parse_joy(msg.paddles[0])
            self.gripper_left.set_position(
                100 * (1 - msg.paddles[0].trigger))
            self.gripper_right.set_position(
                100 * (1 - msg.paddles[1].trigger))

    def _terminate_if_pressed(self, hydra):
        if(sum(hydra.paddles[0].buttons[1:] + hydra.paddles[1].buttons[1:])):
            self._cleanup()

    def _cleanup(self):
        rospy.loginfo("Disabling robot... ")
        self.rs.disable()
        self.mover_left.stop_thread()
        self.mover_right.stop_thread()


if __name__ == '__main__':
    Teleop()