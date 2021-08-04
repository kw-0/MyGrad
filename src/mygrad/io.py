import numpy as np
import typing
import mygrad as mg
from mygrad.tensor_base import tensor, Tensor


def save(filename : str, tensor : Tensor) -> None:
    """Saves a tensor and its gradient information.

    This docstring was adapted from that of numpy.save()

    Parameters
    ----------
    file_name : str
        The desired name of the file that will hold the tensor data. Note that the file will be saved as a .npz

    tensor : Tensor
        The tensor that is to be saved, along with its gradient information. If there is no gradient, it saves as None.

    Returns
    -------
    None
    """
    if not isinstance(tensor, Tensor):
        raise TypeError(f"mygrad.save requires a Tensor-type object, got type {type(tensor)}")

    np.savez(filename, data=tensor.data, grad=tensor.grad)


def load(tensor_filename : str) -> Tensor:
    """Loads a saved Tensor and its gradient information (if applicable).

    This docstring was adapted from that of numpy.load()

    Parameters
    ----------
    tensor_filename : str
        The name of the file that holds the tensor data to load.
    
    Returns
    -------
    A tensor with the desired gradient data.
    """
    _tensor = np.load(tensor_filename)

    loaded_tensor = tensor(_tensor['data'])
    loaded_tensor.backward(_tensor['grad'])

    return loaded_tensor
