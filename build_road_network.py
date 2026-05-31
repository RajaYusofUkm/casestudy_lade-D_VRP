import pandas as pd
import networkx as nx
from shapely import wkt
from shapely.geometry import Point, LineString
import math
import json
from pyproj import Transformer
from scipy.spatial import cKDTree
from datasets import load_dataset
import datetime

print("1. Loading VRP delivery nodes...")
import sys
TARGET_DATE = 708
TARGET_COURIER = 1043

try:
    NUM_NODES = int(sys.argv[1])
except (IndexError, ValueError):
    NUM_NODES = 15

print(f"Building matrix graph for {NUM_NODES} customers...")

dataset = load_dataset("Cainiao-AI/LaDe-D", split="delivery_sh")
df_nodes = dataset.to_pandas()
df_subset = df_nodes[(df_nodes['ds'] == TARGET_DATE) & (df_nodes['courier_id'] == TARGET_COURIER)].head(NUM_NODES).reset_index()

nodes_wgs84 = {'Depot': (121.50500, 31.08500)}
for i, row in df_subset.iterrows():
    nodes_wgs84[f"N{i+1}"] = (float(row['lng']), float(row['lat']))

# Convert to EPSG:3857
transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
nodes_epsg3857 = {}
for name, coords in nodes_wgs84.items():
    x, y = transformer.transform(coords[0], coords[1])
    nodes_epsg3857[name] = (x, y)
    print(f"{name} -> EPSG:3857 ({x:.2f}, {y:.2f})")

print("\n2. Reading roads_shanghai.csv and assigning speeds (Class-Based Imputation)...")
df_roads = pd.read_csv('roads_shanghai.csv', sep='\t')
df_roads['maxspeed'] = pd.to_numeric(df_roads['maxspeed'], errors='coerce').fillna(0)

class_speeds = {
    'motorway': 95.0, 'trunk': 70.6, 'primary': 59.0, 'tertiary_link': 53.3,
    'secondary': 51.4, 'primary_link': 47.3, 'trunk_link': 45.8, 'motorway_link': 41.5,
    'tertiary': 40.8, 'secondary_link': 37.5, 'cycleway': 35.0, 'residential': 24.7,
    'unclassified': 23.6, 'service': 18.4, 'living_street': 17.0, 'path': 15.0, 'pedestrian': 9.0,
    # Unpaved tracks & non-motorised paths (previously silently used the 30 km/h default)
    'track': 15.0, 'track_grade1': 20.0, 'track_grade2': 15.0, 'track_grade3': 12.0,
    'track_grade5': 5.0, 'footway': 5.0, 'steps': 2.0, 'bridleway': 5.0
}
default_speed = 30.0 # fallback for any unrecognised / unknown class

def get_speed(row):
    if row['maxspeed'] > 0:
        return float(row['maxspeed'])
    return class_speeds.get(row['fclass'], default_speed)

df_roads['assigned_speed'] = df_roads.apply(get_speed, axis=1)

print("3. Building the road network graph (NetworkX)...")
G = nx.DiGraph()

for idx, row in df_roads.iterrows():
    try:
        geom = wkt.loads(row['geometry'])
    except:
        continue
        
    if not isinstance(geom, LineString):
        continue
        
    coords = list(geom.coords)
    oneway = row['oneway']
    speed_kmh = row['assigned_speed']
    fclass = row['fclass']

    for i in range(len(coords) - 1):
        u = coords[i]
        v = coords[i+1]

        # Distance in meters (EPSG:3857 is in meters)
        dist_m = math.sqrt((u[0] - v[0])**2 + (u[1] - v[1])**2)
        if dist_m == 0: continue

        time_hours = (dist_m / 1000.0) / speed_kmh
        dist_km = dist_m / 1000.0

        # HARD CONSTRAINT ENFORCEMENT:
        # Add edges according to oneway direction (store fclass & speed for the per-leg breakdown)
        # This mathematically guarantees illegal traffic flow is un-traversable in the DiGraph.
        if oneway == 'F':
            G.add_edge(u, v, weight=time_hours, length=dist_km, fclass=fclass, speed=speed_kmh)
        elif oneway == 'T':
            G.add_edge(v, u, weight=time_hours, length=dist_km, fclass=fclass, speed=speed_kmh)
        else: # 'B' or others
            G.add_edge(u, v, weight=time_hours, length=dist_km, fclass=fclass, speed=speed_kmh)
            G.add_edge(v, u, weight=time_hours, length=dist_km, fclass=fclass, speed=speed_kmh)

print(f"Graph built with {G.number_of_nodes()} junction nodes and {G.number_of_edges()} road edges.")

print("\n4. Finding nearest junctions (Nearest Node Mapping) using KDTree...")
graph_nodes = list(G.nodes())
kdtree = cKDTree(graph_nodes)

node_mapping = {}
for name, target_coords in nodes_epsg3857.items():
    dist, idx = kdtree.query(target_coords)
    mapped_node = graph_nodes[idx]
    node_mapping[name] = mapped_node
    print(f"{name} mapped to nearest junction at {dist:.2f} meters.")

print("\n5. Running Dijkstra for the 16x16 matrix...")
# Compute the shortest path from each mapped node to every other mapped node
node_names = list(nodes_wgs84.keys())

time_matrix = {}
dist_matrix = {}
speed_breakdown = {}  # u -> v -> [ {fclass, dist_km, time_h, speed_kmh}, ... ]
path_geometry = {}    # u -> v -> {coords: [[x,y],...] EPSG:3857, fclass: [per-segment]}

for u_name in node_names:
    time_matrix[u_name] = {}
    dist_matrix[u_name] = {}
    speed_breakdown[u_name] = {}
    path_geometry[u_name] = {}
    u_graph_node = node_mapping[u_name]

    # Run Dijkstra from u to all nodes
    # Since the graph may be disconnected (disconnected components),
    # we must handle the NetworkXNoPath error
    for v_name in node_names:
        if u_name == v_name:
            time_matrix[u_name][v_name] = 0.0
            dist_matrix[u_name][v_name] = 0.0
            speed_breakdown[u_name][v_name] = []
            path_geometry[u_name][v_name] = {'coords': [], 'fclass': []}
            continue

        v_graph_node = node_mapping[v_name]
        try:
            # Shortest path by 'weight' (travel time)
            path = nx.shortest_path(G, source=u_graph_node, target=v_graph_node, weight='weight')

            total_time = 0.0
            total_dist = 0.0
            leg_classes = {}  # fclass -> [dist_km, time_h]
            for k in range(len(path)-1):
                edge_data = G[path[k]][path[k+1]]
                total_time += edge_data['weight']
                total_dist += edge_data['length']
                agg = leg_classes.setdefault(edge_data['fclass'], [0.0, 0.0])
                agg[0] += edge_data['length']
                agg[1] += edge_data['weight']

            # Breakdown by fclass (sorted by descending distance); effective speed = distance/time
            breakdown = [
                {'fclass': fc, 'dist_km': d, 'time_h': t,
                 'speed_kmh': (d / t) if t > 0 else 0.0}
                for fc, (d, t) in sorted(leg_classes.items(), key=lambda x: -x[1][0])
            ]

            time_matrix[u_name][v_name] = total_time
            dist_matrix[u_name][v_name] = total_dist
            speed_breakdown[u_name][v_name] = breakdown
            # Save the actual traversed path geometry (EPSG:3857) + per-segment fclass
            path_geometry[u_name][v_name] = {
                'coords': [[round(px, 1), round(py, 1)] for (px, py) in path],
                'fclass': [G[path[k]][path[k+1]]['fclass'] for k in range(len(path) - 1)]
            }
        except nx.NetworkXNoPath:
            # If there is no connected path (disconnected components),
            # fall back to straight-line (air) distance
            print(f"WARNING: No path found from {u_name} to {v_name}! Falling back to straight-line distance.")
            euclidean_km = math.sqrt((nodes_wgs84[u_name][0] - nodes_wgs84[v_name][0])**2 + (nodes_wgs84[u_name][1] - nodes_wgs84[v_name][1])**2) * 100
            time_matrix[u_name][v_name] = euclidean_km / 20.0 # fallback speed 20
            dist_matrix[u_name][v_name] = euclidean_km
            speed_breakdown[u_name][v_name] = [
                {'fclass': '(air-distance fallback)', 'dist_km': euclidean_km,
                 'time_h': euclidean_km / 20.0, 'speed_kmh': 20.0}
            ]
            path_geometry[u_name][v_name] = {
                'coords': [list(node_mapping[u_name]), list(node_mapping[v_name])],
                'fclass': ['(air-distance fallback)']
            }

print("\n6. Saving the resulting matrices to matrix.json...")
output_data = {
    'nodes_epsg3857': nodes_epsg3857,
    'time_matrix': time_matrix,
    'distance_matrix': dist_matrix,
    'speed_breakdown': speed_breakdown,
    'path_geometry': path_geometry
}

with open('matrix.json', 'w') as f:
    json.dump(output_data, f, indent=4)

print("Done! matrix.json created successfully.")
