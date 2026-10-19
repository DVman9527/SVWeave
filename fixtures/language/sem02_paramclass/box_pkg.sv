package box_pkg;
  class box #(int W = 8, type T = logic);
    bit [W-1:0] data;
    T item;
    function int width();
      return W;
    endfunction
  endclass
endpackage
