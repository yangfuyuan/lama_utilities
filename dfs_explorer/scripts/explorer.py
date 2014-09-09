#!/usr/bin/python
# -*- coding: utf-8 -*-

# Implement a Depth-First Search explorer.
# Works in combination with lj_laser_heading, nj_laser, and nj_escape_crossing
# jockeys.

from __future__ import print_function, division

from math import pi

import rospy
import actionlib
from std_msgs.msg import Float32

from lama_interfaces.msg import NavigateAction
from lama_interfaces.msg import NavigateGoal
from lama_interfaces.msg import LocalizeAction
from lama_interfaces.msg import LocalizeGoal
from lama_interfaces.srv import ActOnMap
from lama_interfaces.srv import ActOnMapRequest
from lama_interface import interface_factory

# TODO: add a mechanism for the robot not to come back to the vertex it comes
# from.

g_max_similarity_for_same = 0.1


def normalize_angles(angles):
    def normalize_angle(angle):
        return (angle + pi) % (2 * pi) - pi
    return [normalize_angle(a) for a in angles]


class ExplorerNode(object):
    def __init__(self):
        # Node and server initialization.
        rospy.init_node('dfs_explorer', log_level=rospy.DEBUG)
        navigating_jockey_name = rospy.get_param('navigating_jockey_name',
                                                 'navigating_jockey')
        localizing_jockey_name = rospy.get_param('localizing_jockey_name',
                                                 'localizing_jockey')
        escape_jockey_name = rospy.get_param('escape_jockey_name',
                                             'nj_escape_jockey')

        # Navigate jockey server.
        self.navigate = actionlib.SimpleActionClient(navigating_jockey_name,
                                                     NavigateAction)
        rospy.logdebug('Waiting for the navigating jockey action server')
        self.navigate.wait_for_server()
        rospy.logdebug('Communicating with the navigating jockey action server')

        # Localize jockey server.
        self.localize = actionlib.SimpleActionClient(localizing_jockey_name,
                                                     LocalizeAction)
        rospy.logdebug('Waiting for the localizing jockey action server')
        self.localize.wait_for_server()
        rospy.logdebug('Communicating with the localizing jockey action server')

        # Crossing escape jockey server.
        self.escape = actionlib.SimpleActionClient(escape_jockey_name,
                                                   NavigateAction)
        rospy.logdebug('Waiting for the crossing escape jockey action server')
        self.escape.wait_for_server()
        rospy.logdebug('Commnunicating with the crossing ' +
                       'escape jockey action server')

        # Map agent server.
        self.map_agent = rospy.ServiceProxy('lama_map_agent', ActOnMap)

        # Descriptor getter for Crossing.
        self.crossing_interface_name = rospy.get_param(
            'crossing_interface_name', 'lj_laser_crossing')
        exits_iface = interface_factory(
            self.crossing_interface_name,
            'lama_interfaces/GetCrossing',
            'lama_interfaces/SetCrossing')
        self.crossing_getter = exits_iface.getter_service_proxy

        # Exit angles getter and setter (double).
        exit_angles_interface_name = rospy.get_param(
            'exit_angles_interface_name',
            'dfs_explorer_exit_angles')
        exits_iface = interface_factory(
            exit_angles_interface_name,
            'lama_interfaces/GetDouble',
            'lama_interfaces/SetDouble')
        self.exit_angles_getter = exits_iface.getter_service_proxy
        self.exit_angles_setter = exits_iface.setter_service_proxy

        # Exit angle topic advertiser.
        exit_angle_topic_name = rospy.get_param("exit_angle_topic",
                                                "exit_angle")
        self.exit_angle_publisher = rospy.Publisher(exit_angle_topic_name,
                                                    Float32,
                                                    queue_size=1,
                                                    latch=True)

        self.at_first_crossing = False
        self.first_vertex = None
        self.last_vertex = None
        self.exit_taken = None
        self.next_vertex = None
        self.next_exit = None
        # The graph is organized as a map (vertex: list(vertex, exit_angle)).
        # Where the second vertex is the vertex that will be at the next
        # crossing center when traversing edge (corridor) at absolute and
        # exit_angle.
        # When starting to traverse an edge, vertex is set to None. A vertex
        # will be visited when all its neighbor vertices are not None.
        # The graph is then an oriented graph where the information for edge
        # a to b is the exit angle that was taken from a to reach b.
        self.graph = {}

        # For each vertex, store its radius so that we know how far to go from
        # the crossing center when we move without navigating jockey.
        self.crossing_radii = {}

    def move_to_first_crossing(self):
        """Move the robot to the first crossing
        Move the robot to the first crossing so that we can have a descriptor
        list to start with with the DFS algorithm.
        """
        nav_goal = NavigateGoal()
        nav_goal.action = nav_goal.TRAVERSE
        self.navigate.send_goal(nav_goal)
        self.navigate.wait_for_result()
        nav_result = self.navigate.get_result()
        if nav_result.final_state == nav_result.DONE:
            rospy.logdebug('Traversed to crossing center in ' +
                           '{:.2f} s'.format(
                               nav_result.completion_time.to_sec()))
        else:
            rospy.logerr('Something wrong happened, exiting!')
            raise Exception('Something wrong happened, exiting!')
        self.at_first_crossing = True

    def loop(self):
        """Run the DFS algorithm until all vertices are visited

        Procedure:
        1. Get a new vertex descriptor when finished traversing, in the case
           that the vertex was not already visited.
        2. Choose the vertex with the next exit to visit and the direction to
           move with DFS.
        3. Move to that vertex.
        4. Let the robot escape from the node in the chosen direction.
        5. Let the navigating jockey move to the next crossing.
        6. Repeat from 1. indefinitely.
        """
        if not self.at_first_crossing:
            rospy.logerr('Go to first crossing first')
            raise Exception('Go to first crossing first')

        while True:
            # 1. Get a new vertex descriptor when finished traversing.
            self.get_descriptor()

            # 2. Choose the vertex with the next exit to visit
            vertex_and_angle = self.get_next_vertex_to_visit()
            if vertex_and_angle is None:
                rospy.loginfo('I visisted all crossings, successfully exiting')
                break
            self.next_vertex, self.next_exit = vertex_and_angle

            # 3. Move to that vertex.
            self.move_to_next_crossing()

            # 4. Let the robot escape from the node in the chosen direction.
            # The edge does not exists yet, set the direction through a topic.
            self.escape_from_crossing()

            # 5. Let the navigating jockey move to the next crossing.
            self.move_to_first_crossing()

    def get_descriptor(self):
        """Get the descriptors from the current crossing center

        Get the descriptors.
        Push the vertex.
        Add vertices to the graph.

        Parameters
        ----------
        - origin_angle: float, absolute angle of the exit the robot took
            when starting from origin_vertex.
        """
        loc_goal = LocalizeGoal()
        loc_goal.action = loc_goal.GET_VERTEX_DESCRIPTOR
        self.localize.send_goal_and_wait(loc_goal, rospy.Duration(0.5))
        loc_result = self.localize.get_result()
        if not loc_result:
            rospy.logerr('Did not receive vertex descriptor within ' +
                         '0.5 s, exiting')
            break
        rospy.logdebug('Received vertex descriptor')
        # The LaserScan and the exit_ angles are the 1st and 3rd descriptors
        # respectively.
        self.add_vertex(loc_result.descriptors)

    def add_vertex(self, descriptors):
        vertices, similarities = self.get_similarity()
        vertex_is_new = True
        if similarities and (min(similarities) < g_max_similarity_for_same):
            vertex_is_new = False
        if vertex_is_new:
            # Add vertex to map.
            map_action = ActOnMapRequest()
            map_action.action.action = map_action.action.PUSH_VERTEX
            response = self.map_agent(map_action)
            new_vertex = response.object.id
            # Assign descriptors.
            map_action = ActOnMapRequest()
            map_action.object.id = new_vertex
            map_action.action.action = (
                map_action.action.ASSIGN_DESCRIPTOR_VERTEX)
            for descriptor in descriptors:
                map_action.descriptor.descriptor_id = descriptor.descriptor_id
                self.map_agent(map_action)
            # Get the exit_angles from map.
            # TODO: Don't use magic numbers.
            # Crossing is the 2nd descriptor.
            crossing = self.crossing_getter(descriptors[1].descriptor_id)
            self.crossing_radii[new_vertex] = crossing.radius
            # Add vertex and associate the sorted list of [None, angle].
            nodes = [[None, f.angle] for f in crossing.frontiers]
            self.graph[new_vertex] = sorted(nodes)
            self.add_edge_to_graph(new_vertex)
            self.last_vertex = new_vertex
        else:
            index_vertex_same = vertices.index(min(similarities))
            vertex_same = vertices(index_vertex_same)
            self.add_edge_to_graph(vertex_same)
            self.last_vertex = vertex_same
        if not self.first_vertex:
            self.first_vertex = self.last_vertex

    def get_similarity(self):
        loc_goal = LocalizeGoal()
        loc_goal.action = loc_goal.GET_SIMILARITY
        self.localize.send_goal_and_wait(loc_goal, rospy.Duration(0.5))
        loc_result = self.localize.get_result()
        if not loc_result:
            rospy.logerr('Did not received vertex descriptor within ' +
                         '0.5 s, exiting')
            break
        rospy.logdebug('Received vertex descriptor')
        return loc_result.idata, loc_result.fdata

    def add_edge_to_graph(self, vertex):
        """Add an edge from self.last_vertex to vertex

        This means replace None with vertex for the adjacent vertex of
        self.last_vertex for which the edge information is self.exit_taken.
        """
        # TODO: rebuild the graph from the map...
        # so that several robots can be used simultaneously.
        if not self.last_vertex:
            return
        old_nodes = self.graph[self.last_vertex]
        new_nodes = []
        for v, a in old_nodes:
            if a == self.exit_taken:
                new_nodes.append([vertex, a])
                self.add_edge_to_map(self.last_vertex, vertex, self.exit_taken)
            else:
                new_nodes.append([v, a])
        self.graph[self.last_vertex] = new_nodes

    def add_edge_to_map(self, v0, v1, exit_angle):
        """Add an edge and its associated descriptor to the map

        The oriented edge is from v0 to v1.
        The edge descriptor is the exit angle to take at v0 to go to v1.
        """
        # Add edge.
        map_action = ActOnMapRequest()
        map_action.action.action = map_action.PUSH_EDGE
        map_action.object.type = map_action.object.EDGE
        map_action.object.references.append(v0)
        map_action.object.references.append(v1)
        edge_response = self.map_agent(map_action)
        # Add descriptor.
        desc_response = self.exit_angles_setter(exit_angle)
        # Assign descriptor.
        map_action = ActOnMapRequest()
        map_action.action.action = map_action.action.ASSIGN_DESCRIPTOR_EDGE
        map_action.descriptor.object_id = edge_response.id
        map_action.descriptor.descriptor_id = desc_response.id
        self.map_agent(map_action)

    def get_next_vertex_to_visit(self):
        """Return the tuple (vertex, angle)
        """
        def next_known(nodes):
            for v, _ in nodes:
                if (v is not None) and (v not in discovered):
                    return v
            return None

        def first_unknown(nodes):
            for v, a in nodes:
                if v is None:
                    return v, a
            return None

        stack = [self.first_vertex]
        discovered = []
        while stack:
            nodes = self.graph[stack[-1]]
            v = next_known(nodes)
            if v is None:
                if first_unknown(nodes) is None:
                    discovered.append(stack.pop())
                else:
                    return first_unknown(nodes)
        return None

    def move_to_next_crossing(self):
        # TODO: remove angle from find_path output
        path = self.find_path()
        for vertex, angle in path:
            # Escape from crossing center.
            goal = NavigateGoal()
            goal.action = goal.TRAVERSE
            goal.edge.type = goal.edge.EDGE
            goal.edge.id = self.edge_id(self.last_vertex, vertex)
            if goal.edge.id is None:
                err = 'No edge from {} to {}'.format(self.last_vertex, vertex)
                rospy.logfatal(err)
                return False
            result = self.escape.send_goal_and_wait(goal)
            if result.final_state != result.DONE:
                err = 'Escape jockey did not succeed'
                rospy.logerr(err)
                return False
            # Go to next crossing.
            goal = NavigateGoal()
            goal.action = goal.TRAVERSE
            result = self.navigate.send_goal_and_wait(goal)
            if result.final_state != result.DONE:
                err = 'Escape jockey did not succeed'
                rospy.logerr(err)
                return False
            self.last_vertex = vertex
            self.exit_taken = angle
        return True

    def edge_id(self, v0, v1):
        """Return the id of edge from v0 to v1"""
        map_action = ActOnMapRequest()
        map_action.action.action = map_action.action.GET_EDGE_LIST
        response = self.map_agent(map_action)
        for o in response.objects:
            if (o.references[0] == v0) and (o.references[1] == v1):
                return o.id
        return None

    def find_path(self):
        """Return a list of (vertex, angle)

        self.last_vertex (the crossing the robot presently is in) will not be
        part of the path. The last vertex will be self.next_vertex.
        """
        # TODO: remove angle from find_path output
        def adjacent_vertices(vertex):
            vertices = []
            for v, a in self.graph[vertex]:
                vertices.append(v)

        def path_edges(edges, end):
            edges = path_edges(edges, end)
            for v1, v0 in edges:
                if v1 == end:
                    return path(edges, v1) + [v0]

        def transform_graph(ingraph):
            graph = {}
            for k, node in ingraph.iteritems():
                for v, a in node:
                    graph[(k, v)] = a
            return graph

        def path(edges, end):
            edges = path_edges(edges, end)
            graph = transform_graph(self.graph)
            v0 = edges.pop()
            p = []
            while edges:
                v1 = edges.pop()
                p.append((v1, graph[(v0, v1)]))
            return p

        # TODO: use a BrowseVertex that has as attributes the BrowseVertex
        # it comes from during browsing and the associated angle so that
        # after the dfs-tree is built, the path can be found by browsing the
        # referenced vertices.
        start = self.last_vertex
        end = self.next_vertex
        queue = [start]
        discovered = set()
        discovered.add(start)
        edges = []
        while queue:
            vertex = queue.pop(0)
            if vertex == end:
                return path(edges, end)
            for v in adjacent_vertices(vertex):
                if v not in discovered:
                    discovered.add(v)
                    edges.add((v, vertex))
                    queue.append(v)
        return None

    def get_crossing_desc_id(self, vertex):
        """Return the first Crossing descriptor associated with vertex"""
        map_action = ActOnMapRequest()
        map_action.action.action = map_action.action.PULL_VERTEX
        map_action.object.id = vertex
        response = self.map_agent(map_action)
        for d in response.descriptors:
            if d.interface_name == self.crossing_interface_name:
                return response.descriptors.descriptor_id

    def escape_from_crossing(self):
        """Escape from crossing towards an unknown edge and return when done"""
        self.exit_angle_publisher.publish(self.next_exit)
        nav_goal = NavigateGoal()
        nav_goal.action = nav_goal.action.TRAVERSE
        nav_goal.descriptor.descriptor_id = self.get_crossing_desc_id(
            self.next_vertex)
        self.escape.send_goal_and_wait(nav_goal)
        escape_result = self.escape.get_result()
        if escape_result != escape_result.DONE:
            err = 'Escape jockey did not succeed'
            rospy.logerr(err)
            raise Exception(err)

node = ExplorerNode()
node.move_to_first_crossing()
node.loop()