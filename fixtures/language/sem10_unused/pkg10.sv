package pkg10;
  function automatic int helper(int x);
    return x + 1;
  endfunction

  class used_cls;
    function int f();
      return helper(1);
    endfunction
  endclass

  class gen_unused #(type T = int);
    function int g();
      return helper(2);
    endfunction
  endclass
endpackage
