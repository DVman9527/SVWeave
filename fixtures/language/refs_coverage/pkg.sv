package cov_pkg;
  parameter int WIDTH = 8;
  typedef logic [7:0] return_t;

  function automatic return_t make_value();
    return 8'h5a;
  endfunction

  class unused_generic #(type T = int);
    function int read_width();
      return WIDTH;
    endfunction
  endclass
endpackage
