# RM65-B analytic IK source

Vendored from the project `rm65b-swik`, commit
`16ec60620aa0bcdd58239c16f34aebd33689681d`.
The original repository does not declare a distribution license.

The solver modules retain the upstream implementation. Local integration adds
`ros_node.py`, ROS package metadata and service tests. `config_loader.py` resolves
the model from this workspace's central `config/ros/rm65b_analytic_ik.yaml`,
`REALMAN_CONFIG_ROOT`, or the installed package share directory. The original
solver tests are retained with their configuration fixture path adapted.
`resource/rm65b_analytic_ik.yaml` is an installation symlink to that central model.

Update the solver and its tests together from a reviewed upstream commit.
