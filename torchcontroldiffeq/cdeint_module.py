import torch
import torchdiffeq

from . import path


class _VectorField(torch.nn.Module):
    def __init__(self, X, func, t_requires_grad, z0_requires_grad, adjoint):
        """Defines a controlled vector field.

        Arguments:
            X: As cdeint.
            func: As cdeint.
            t_requires_grad: Whether the 't' argument to cdeint requires gradient.
            z0_requires_grad: Whether the 'z0' argument to cdeint requires gradient.
            adjoint: Whether we are using the adjoint method.
        """
        super(_VectorField, self).__init__()
        if not isinstance(func, torch.nn.Module):
            raise ValueError("func must be a torch.nn.Module.")

        self.X = X
        self.func = func
        self.t_not_requires_grad = adjoint and not t_requires_grad
        self.z_not_requires_grad = adjoint and not z0_requires_grad

    def parameters(self):
        # Makes sure that the adjoint method sees relevant non-leaf tensors to compute derivatives wrt to.
        yield from self.parameters()
        yield from self.X._controldiffeq_computed_parameters.values()

    def __call__(self, t, z):
        # Use tupled input to avoid torchdiffeq doing it for us and breaking the parameters() we've created above.
        z = z[0]

        # So what's up with this then?
        #
        # First of all, this only applies if we're using the adjoint method, so this doesn't change anything in the
        # non-adjoint case. In the adjoint case, however, the derivative wrt t is only used to compute the derivative
        # wrt the input times, and the derivative wrt z is only used to compute the derivative wrt the initial z0.
        #
        # By default torchdiffeq computes all of these gradients regardless, and any ones that aren't needed just get
        # discarded. So for one thing, detaching here gives us a speedup.
        #
        # More importantly, however: the fact that it's computing these gradients affects adaptive step size solvers, as
        # the solver tries to resolve the gradients wrt these additional arguments. In the particular case of linear
        # interpolation, this poses a problem, as the derivative wrt t doesn't exist. (Or rather, it's measure-valued,
        # which is the same thing as far as things are concerned here.) This breaks the adjoint method.
        #
        # As it's generally quite rare to compute derivatives wrt the times, this is the fix: most of the time we just
        # tell torchdiffeq that we don't have a gradient wrt that input, so it doesn't bother calculating it in the
        # first place.
        #
        # (And if you do want gradients wrt times - just don't use linear interpolation!)
        if self.t_not_requires_grad:
            t = t.detach()
        # Similar story for gradients wrt z. In this case we don't have things actually breaking; this is just a
        # speed-up.
        if self.z_not_requires_grad:
            z = z.detach()

        # control_gradient is of shape (..., input_channels)
        control_gradient = self.X.derivative(t)
        # vector_field is of shape (..., hidden_channels, input_channels)
        vector_field = self.func(z)
        # out is of shape (..., hidden_channels)
        # (The squeezing is necessary to make the matrix-multiply properly batch in all cases)
        out = (vector_field @ control_gradient.unsqueeze(-1)).squeeze(-1)
        return (out,)


def cdeint(X, z0, func, t, adjoint=True, **kwargs):
    r"""Solves a system of controlled differential equations.

    Solves the controlled problem:
    ```
    z_t = z_{t_0} + \int_{t_0}^t f(z_s) dX_s
    ```
    where z is a tensor of any shape, and X is some controlling signal.

    Arguments:
        X: The control. This should be a instance of a subclass of `torchcontroldiffeq.Path`, for example
            `torchcontroldiffeq.NaturalCubicSpline`. This represents a continuous path derived from the data. (Or from
            anything else; e.g. the changing hidden states of a Neural CDE.) The derivative at a point will be computed
            via this argument, and will have shape (..., input_channels), where '...' is some number of batch dimensions
            and input_channels is the number of channels in the input path.
        z0: The initial state of the solution. It should have shape (..., hidden_channels), where '...' is some number
            of batch dimensions.
        func: Should be an instance of `torch.nn.Module`. Describes the vector field f(z). Will be called with a tensor
            z of shape (..., hidden_channels), and should return a tensor of shape
            (..., hidden_channels, input_channels), where hidden_channels and input_channels are integers defined by the
            `hidden_shape` and `X` arguments as above. The '...' corresponds to some number of batch dimensions.
        t: a one dimensional tensor describing the times to range of times to integrate over and output the results at.
            The initial time will be t[0] and the final time will be t[-1].
        adjoint: A boolean; whether to use the adjoint method to backpropagate.
        **kwargs: Any additional kwargs to pass to the odeint solver of torchdiffeq. Note that empirically, the solvers
            that seem to work best are dopri5, euler, midpoint, rk4. Avoid all three Adams methods.

    Returns:
        The value of each z_{t_i} of the solution to the CDE z_t = z_{t_0} + \int_0^t f(z_s)dX_s, where t_i = t[i]. This
        will be a tensor of shape (len(t), ..., hidden_channels).

    Raises:
        ValueError for malformed inputs.

    Warning:
        Don't use the particular combination of adjoint=True (the default), and linear interpolation to construct X.
        It doesn't work. (For mathematical reasons: the adjoint method requires access to d2X_dt2, which is
        measure-valued for linear interpolation.)
    """

    if not isinstance(X, path.Path):
        raise ValueError("X must be an instance of torchcontroldiffeq.Path so that we can find the parameters we need "
                         "to differentiate with respect to. Make sure that all leaf tensors requiring gradient are "
                         "registered with the Path as a torch.nn.Parameter, and all the non-leaf tensors requiring "
                         "gradient are registered with the Module as a torchcontroldiffeq.ComputedParameter.")
    control_gradient = X.derivative(t[0].detach())
    if control_gradient.shape[:-1] != z0.shape[:-1]:
        raise ValueError("X.derivative did not return a tensor with the same number of batch dimensions as z0. "
                         "X.derivative returned shape {} (meaning {} batch dimensions)), whilst z0 has shape {} "
                         "(meaning {} batch dimensions)."
                         "".format(tuple(control_gradient.shape), tuple(control_gradient.shape[:-1]), tuple(z0.shape),
                                   tuple(z0.shape[:-1])))
    vector_field = func(z0)
    if vector_field.shape[:-2] != z0.shape[:-1]:
        raise ValueError("func did not return a tensor with the same number of batch dimensions as z0. func returned "
                         "shape {} (meaning {} batch dimensions)), whilst z0 has shape {} (meaning {} batch"
                         " dimensions)."
                         "".format(tuple(vector_field.shape), tuple(vector_field.shape[:-2]), tuple(z0.shape),
                                   tuple(z0.shape[:-1])))
    if vector_field.size(-2) != z0.shape[-1]:
        raise ValueError("func did not return a tensor with the same number of hidden channels as z0. func returned "
                         "shape {} (meaning {} channels), whilst z0 has shape {} (meaning {} channels)."
                         "".format(tuple(vector_field.shape), vector_field.size(-2), tuple(z0.shape),
                                   z0.shape.size(-1)))
    if vector_field.size(-1) != control_gradient.size(-1):
        raise ValueError("func did not return a tensor with the same number of input channels as X.derivative "
                         "returned. func returned shape {} (meaning {} channels), whilst X.derivative returned shape "
                         "{} (meaning {} channels)."
                         "".format(tuple(vector_field.shape), vector_field.size(-1), tuple(control_gradient.shape),
                                   control_gradient.size(-1)))

    vector_field = _VectorField(X=X, func=func, t_requires_grad=t.requires_grad, z0_requires_grad=z0.requires_grad,
                                adjoint=adjoint)
    odeint = torchdiffeq.odeint_adjoint if adjoint else torchdiffeq.odeint
    # Note how we pass in a tuple to avoid torchdiffeq wrapping vector_field in something that isn't computed-parameter
    # aware.
    # I don't like depending on an implementation detail of torchdiffeq like this but I don't see many other options
    # that don't involve directly modifying torch.nn.Module (probably best to stay away from that).
    out = odeint(func=vector_field, y0=(z0,), t=t, **kwargs)

    return out